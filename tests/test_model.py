'''
模型：用合成的打标数据训练、检验和打分（"喜欢"的影片都带 g0 和 s0 两个标签，两类可以完全分开），
以及训练条件、模型文件的保存和读取、旧版本程序留下的模型文件、训练与打分同时进行等情况
'''
import builtins
import datetime
import errno
import json
import logging
import math
import os
import pickle
import random
import re
import sys
import threading
import types
import warnings

import pytest
from peewee import OperationalError
from sklearn.feature_extraction.text import TfidfTransformer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import MultiLabelBinarizer

from bustag import util
from bustag.model import classifier, persist, prepare
from bustag.model.persist import LegacyModelError, ModelLoadError, ModelSaveError, Report, SavedModel
from bustag.spider import db
from bustag.spider.db import RATE_TYPE, RATE_VALUE, ItemRate, ItemScore

GENRES = [f'g{i}' for i in range(1, 20)]
STARS = [f's{i}' for i in range(1, 30)]
# 程序里交叉验证重复的次数（导入时取，测试期间它被改成 2，见 conftest 的 quick_validation）
DEFAULT_REPEATS = classifier.CV_REPEATS
NOW = datetime.datetime(2026, 11, 6, 12, 0, 0)
REPORT = Report(likes=150, dislikes=60, folds=5, repeats=10, like_recall=0.9, dislike_recall=0.8, auc=0.95)
SKLEARN_WARNINGS = ('column-vector', 'ill-defined', 'zero_division', 'unknown class', 'converge', 'penalty')
# 一份内容正确的模型文件
VALID = {'version': 1, 'trained_at': '2026-11-06 12:00:00',
         'report': {'likes': 150, 'dislikes': 60, 'folds': 5, 'repeats': 10, 'like_recall': 0.9,
                    'dislike_recall': 0.8, 'auc': 0.95},
         'intercept': 0.5, 'tags': {'g1': [1.5, 0.25], 's1': [2.0, -0.75]}}


@pytest.fixture
def movies(make_item):
    '''
    生成影片，返回番号列表。like=True 的影片带 g0 和 s0；
    rated=True 时按 like 写入用户打标
    '''
    rng = random.Random(1)
    counter = iter(range(1, 10000))

    def make(count, like, rated=True):
        fanhaos = []
        with db.db.atomic():
            for _ in range(count):
                fanhao = f'MOD-{next(counter):04d}'
                genres = rng.sample(GENRES, 3) + (['g0'] if like else [])
                stars = rng.sample(STARS, 2) + (['s0'] if like else [])
                make_item(fanhao, tags=[('genre', g) for g in genres] + [('star', s) for s in stars])
                if rated:
                    value = RATE_VALUE.LIKE if like else RATE_VALUE.DISLIKE
                    ItemRate.saveit(RATE_TYPE.USER_RATE, value, fanhao)
                fanhaos.append(fanhao)
        return fanhaos
    return make


def model_file(data_dir):
    return data_dir / persist.MODEL_FILE


def write_content(data_dir, content):
    '''把 content（还没转成 JSON 的内容）写成模型文件'''
    model_file(data_dir).write_text(json.dumps(content, ensure_ascii=False), encoding='utf-8')


def model_with(tags, intercept=0.0):
    return SavedModel(intercept=intercept, tags=tags, report=REPORT, trained_at='2026-11-06 12:00:00')


def warning_messages(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def model_folder(data_dir):
    '''model/ 目录里的文件名（检查有没有留下临时文件）'''
    return sorted(path.name for path in (data_dir / 'model').iterdir())


def system_rates():
    rows = ItemRate.select().where(ItemRate.rate_type == RATE_TYPE.SYSTEM_RATE)
    return {row.item_id: row.rate_value for row in rows}


def user_rates():
    '''用户打标的全部内容，用来确认一条都没有变'''
    rows = ItemRate.select().where(ItemRate.rate_type == RATE_TYPE.USER_RATE).order_by(ItemRate.id)
    return [(row.id, row.item_id, row.rate_value, row.rete_time) for row in rows]


def stored_scores():
    return {row.item_id: row for row in ItemScore.select()}


def sigmoid(value):
    return 1 / (1 + math.exp(-value))


class TestData:
    def test_movies_come_with_all_their_tag_values_newest_first(self, make_item):
        make_item('A-001', tags=[('genre', '标签一'), ('star', '演员甲'), ('系列:', '系列甲'), ('製作商:', '同名'),
                                 ('發行商:', '同名')])
        make_item('A-002')
        make_item('A-003', tags=[('genre', '标签一'), ('star', '演员乙')])
        make_item('A-004', tags=[('genre', '标签二')])
        make_item('A-005')
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, 'A-001')
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE, 'A-002')
        ItemRate.saveit(RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE, 'A-003')
        ItemRate.saveit(RATE_TYPE.SYSTEM_RATE, RATE_VALUE.DISLIKE, 'A-004')
        assert prepare.load_labeled() == [(set(), 0), ({'标签一', '演员甲', '系列甲', '同名'}, 1)]
        assert prepare.load_unlabeled() == [('A-005', set()), ('A-004', {'标签二'}), ('A-003', {'标签一', '演员乙'})]
        assert prepare.count_labels() == (1, 1)

    def test_nothing_in_the_library(self):
        assert prepare.load_labeled() == []
        assert prepare.load_unlabeled() == []
        assert prepare.count_labels() == (0, 0)
        assert prepare.has_unlabeled() is False

    def test_whether_a_movie_is_left_that_the_user_has_not_tagged(self, make_item):
        make_item('A-001')
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, 'A-001')
        assert prepare.has_unlabeled() is False
        make_item('A-002')
        assert prepare.has_unlabeled() is True
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE, 'A-002')
        assert prepare.has_unlabeled() is False
        # 只有系统评分的也还没有用户打标
        make_item('A-003')
        ItemRate.saveit(RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE, 'A-003')
        assert prepare.has_unlabeled() is True

    @pytest.mark.parametrize('likes, dislikes, problem', [
        (0, 0, '训练数据不足, 无法训练模型. 需要200, 当前0'),
        (150, 49, '训练数据不足, 无法训练模型. 需要200, 当前199'),
        (200, 0, '"不喜欢"的打标只有 0 条，至少要 10 条才能训练，还差 10 条。请再标一些"不喜欢"的影片'),
        (203, 9, '"不喜欢"的打标只有 9 条，至少要 10 条才能训练，还差 1 条。请再标一些"不喜欢"的影片'),
        (4, 206, '"喜欢"的打标只有 4 条，至少要 10 条才能训练，还差 6 条。请再标一些"喜欢"的影片'),
        (190, 10, None),
        (10, 190, None),
        (100, 100, None),
    ])
    def test_what_is_still_missing_before_training(self, likes, dislikes, problem):
        assert prepare.training_problem(likes, dislikes) == problem

    @pytest.mark.parametrize('likes, dislikes, expected', [(190, 10, True), (171, 29, True), (29, 500, True),
                                                           (170, 30, False), (100, 100, False)])
    def test_few_tags_of_one_kind_are_for_reference_only(self, likes, dislikes, expected):
        assert prepare.for_reference_only(likes, dislikes) is expected

    def test_the_smaller_class(self):
        assert prepare.smaller_class(185, 19) == ('不喜欢', 19)
        assert prepare.smaller_class(19, 185) == ('喜欢', 19)
        assert prepare.smaller_class(100, 100) == ('不喜欢', 100)


class TestTrain:
    def test_train_saves_one_file_with_the_weights_and_the_report(self, movies, data_dir, monkeypatch):
        monkeypatch.setattr(classifier, 'get_now_time', lambda: NOW)
        monkeypatch.setattr(classifier, 'CV_REPEATS', DEFAULT_REPEATS)
        movies(120, like=True)
        movies(120, like=False)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            saved = classifier.train()
        assert not [w for w in caught if any(key in str(w.message) for key in SKLEARN_WARNINGS)]
        report = saved.report
        assert (report.likes, report.dislikes, report.folds, report.repeats) == (120, 120, 5, 10)
        # 两类完全可分，正常都是 1.0；阈值留出余量，容许不同 scikit-learn 版本算出来有出入
        assert report.like_recall >= 0.95 and report.dislike_recall >= 0.95 and report.auc >= 0.98
        assert saved.trained_at == '2026-11-06 12:00:00'
        assert set(saved.tags) == {'g0', 's0', *GENRES, *STARS}
        assert all(idf > 0 for idf, _ in saved.tags.values())
        assert saved.tags['g0'][1] > 1 and saved.tags['s0'][1] > 1
        content = json.loads(model_file(data_dir).read_text(encoding='utf-8'))
        assert set(content) == {'version', 'trained_at', 'report', 'intercept', 'tags'}
        assert content['version'] == 1
        assert content['report'] == {'likes': 120, 'dislikes': 120, 'folds': 5, 'repeats': 10,
                                     'like_recall': report.like_recall, 'dislike_recall': report.dislike_recall,
                                     'auc': report.auc}
        assert content['tags']['g0'] == list(saved.tags['g0'])
        assert model_folder(data_dir) == ['model.json']
        assert classifier.load() == saved

    def test_training_again_on_the_same_tags_gives_the_same_model(self, movies):
        movies(120, like=True)
        movies(120, like=False)
        first, second = classifier.train(), classifier.train()
        assert (second.report, second.tags, second.intercept) == (first.report, first.tags, first.intercept)

    def test_training_log_tells_the_result(self, movies, caplog):
        movies(120, like=True)
        movies(120, like=False)
        caplog.set_level(logging.INFO, logger='bustag')
        classifier.train()
        assert any(re.fullmatch(r'用 240 条打标训练（喜欢 120、不喜欢 120）。交叉验证（5 折 × 2 次）：'
                                r'喜欢认对 \d+%，不喜欢认对 \d+%，排序 AUC [01]\.\d\d', m) for m in caplog.messages)
        assert '模型训练完成' in caplog.messages
        assert warning_messages(caplog) == []

    def test_validation_is_done_on_movies_the_model_was_not_fitted_on(self, make_item):
        '''
        每部影片都有一个只有它才有的标签，其余标签和打标没有关系：拿训练过的影片来检验会接近满分，
        拿没训练过的来检验只能是瞎猜的水平
        '''
        rng = random.Random(7)
        with db.db.atomic():
            for n in range(240):
                fanhao = f'RND-{n:03d}'
                make_item(fanhao, tags=[('genre', genre) for genre in rng.sample(GENRES, 3)] + [('star', f'演员{n}')])
                ItemRate.saveit(RATE_TYPE.USER_RATE, n % 2, fanhao)
        report = classifier.train().report
        assert 0.3 <= report.auc <= 0.7
        assert 0.25 <= report.like_recall <= 0.75 and 0.25 <= report.dislike_recall <= 0.75

    def test_the_saved_model_is_fitted_on_every_tagged_movie(self, movies, make_item):
        movies(120, like=True)
        movies(120, like=False)
        make_item('ONLY-001', tags=[('genre', 'g0'), ('genre', '只有一部影片带的标签')])
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, 'ONLY-001')
        saved = classifier.train()
        assert '只有一部影片带的标签' in saved.tags
        assert (saved.report.likes, saved.report.dislikes) == (121, 120)

    def test_validation_never_splits_into_more_parts_than_the_smaller_class_has_movies(self, movies, monkeypatch):
        monkeypatch.setattr(prepare, 'MIN_MINORITY', 3)
        movies(205, like=True)
        movies(3, like=False)
        assert classifier.train().report.folds == 3

    def test_not_enough_data(self, movies, data_dir):
        movies(50, like=True)
        movies(50, like=False)
        with pytest.raises(ValueError, match=r'^训练数据不足, 无法训练模型\. 需要200, 当前100$'):
            classifier.train()
        assert model_folder(data_dir) == []

    @pytest.mark.parametrize('likes, dislikes, message', [
        (0, 210, '"喜欢"的打标只有 0 条，至少要 10 条才能训练，还差 10 条'),
        (205, 9, '"不喜欢"的打标只有 9 条，至少要 10 条才能训练，还差 1 条'),
    ])
    def test_the_smaller_class_needs_ten_tags(self, movies, data_dir, likes, dislikes, message):
        movies(likes, like=True)
        movies(dislikes, like=False)
        with pytest.raises(ValueError, match=message):
            classifier.train()
        assert model_folder(data_dir) == []

    @pytest.mark.parametrize('dislikes, tentative', [(10, True), (30, False)])
    def test_few_tags_of_one_kind_train_with_a_warning(self, movies, caplog, dislikes, tentative):
        movies(200, like=True)
        movies(dislikes, like=False)
        saved = classifier.train()
        assert saved.report.dislikes == dislikes
        warning = f'"不喜欢"的打标只有 {dislikes} 条（不到 30 条），这个模型的结果仅供参考'
        assert (warning in warning_messages(caplog)) is tentative

    def test_movies_without_any_tags_cannot_train(self, make_item, data_dir):
        with db.db.atomic():
            for n in range(210):
                make_item(f'BARE-{n:03d}')
                ItemRate.saveit(RATE_TYPE.USER_RATE, n % 2, f'BARE-{n:03d}')
        with pytest.raises(ValueError, match='打标的影片都没有标签，无法训练模型'):
            classifier.train()
        assert model_folder(data_dir) == []

    def test_failure_while_evaluating_keeps_the_saved_model(self, movies, make_item, data_dir, monkeypatch):
        movies(120, like=True)
        movies(120, like=False)
        classifier.train()
        before = model_file(data_dir).read_bytes()
        make_item('NEW-001', tags=[('genre', '新类别')])
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, 'NEW-001')

        def broken(*args):
            raise RuntimeError('检验出错')

        monkeypatch.setattr(classifier, '_cross_validate', broken)
        with pytest.raises(RuntimeError, match='检验出错'):
            classifier.train()
        assert model_file(data_dir).read_bytes() == before
        assert model_folder(data_dir) == ['model.json']


class TestScoreTags:
    def test_matches_the_scikit_learn_pipeline_it_was_exported_from(self, movies):
        movies(120, like=True)
        movies(120, like=False)
        saved = classifier.train()
        rows = prepare.load_labeled()
        binarizer = MultiLabelBinarizer(sparse_output=True)
        X = binarizer.fit_transform([tags for tags, _ in rows])
        pipeline = make_pipeline(TfidfTransformer(), LogisticRegression(class_weight='balanced', C=10.0, max_iter=5000))
        pipeline.fit(X, [target for _, target in rows])
        rng = random.Random(3)
        known_and_new = GENRES + STARS + ['g0', 's0', '没见过的标签甲', '没见过的标签乙']
        samples = [set(rng.sample(known_and_new, rng.randint(0, 8))) for _ in range(200)]
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            expected = pipeline.predict_proba(binarizer.transform(samples))[:, 1]
        scores = [classifier.score_tags(saved, tags)[0] for tags in samples]
        assert max(abs(mine - theirs) for mine, theirs in zip(scores, expected)) < 1e-9
        assert min(scores) < 0.2 and max(scores) > 0.8

    def test_only_tags_the_model_knows_count(self):
        model = model_with({'甲': (2.0, 1.5), '乙': (1.0, -0.5)}, intercept=0.25)
        score, _, _ = classifier.score_tags(model, {'甲', '乙', '没见过的标签'})
        assert score == pytest.approx(sigmoid(0.25 + (2.0 * 1.5 - 1.0 * 0.5) / math.sqrt(5)))
        assert classifier.score_tags(model, {'甲'})[0] == pytest.approx(sigmoid(0.25 + 1.5))
        for tags in ({'没见过的标签'}, set()):
            assert classifier.score_tags(model, tags) == (pytest.approx(sigmoid(0.25)), [], [])

    def test_names_the_tags_that_raise_and_lower_the_score_most(self):
        model = model_with({'大加分': (1.0, 3.0), '小加分': (1.0, 1.0), '中加分': (1.0, 2.0), '几乎没影响': (1.0, 0.01),
                            '小减分': (1.0, -1.0), '大减分': (1.0, -4.0), '中减分': (1.0, -2.0)})
        _, because, against = classifier.score_tags(model, set(model.tags))
        assert (because, against) == (['大加分', '中加分'], ['大减分', '中减分'])
        assert classifier.score_tags(model, {'小加分', '几乎没影响'})[1:] == (['小加分'], [])
        assert classifier.score_tags(model, {'小减分', '几乎没影响'})[1:] == ([], ['小减分'])
        assert classifier.score_tags(model, {'几乎没影响'})[1:] == ([], [])

    def test_a_rare_tag_weighs_more_than_a_common_one_with_the_same_coefficient(self):
        model = model_with({'少见': (4.0, 1.0), '常见': (1.0, 1.0)})
        assert classifier.score_tags(model, {'少见', '常见'})[1] == ['少见', '常见']

    def test_extreme_weights_stay_between_zero_and_one(self):
        model = model_with({'极低': (1.0, -5000.0), '极高': (1.0, 5000.0)})
        assert classifier.score_tags(model, {'极低'})[0] == 0.0
        assert classifier.score_tags(model, {'极高'})[0] == 1.0


class TestRecommend:
    def test_scores_every_movie_the_user_has_not_tagged(self, movies, caplog):
        movies(120, like=True)
        movies(120, like=False)
        likely = movies(10, like=True, rated=False)
        unlikely = movies(30, like=False, rated=False)
        # 以前的模型留下的系统评分：不像的判成了喜欢，像的判成了不喜欢
        ItemRate.saveit(RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE, unlikely[0])
        ItemRate.saveit(RATE_TYPE.SYSTEM_RATE, RATE_VALUE.DISLIKE, likely[0])
        classifier.train()
        before = user_rates()
        caplog.set_level(logging.INFO, logger='bustag')
        caplog.clear()

        result = classifier.recommend()
        assert result == (40, 20, 0)
        scores = stored_scores()
        assert set(scores) == set(likely + unlikely)
        assert all(0 < row.score < 1 for row in scores.values())
        assert min(scores[fanhao].score for fanhao in likely) > max(scores[fanhao].score for fanhao in unlikely)
        assert all(set(json.loads(scores[fanhao].because)) == {'g0', 's0'} for fanhao in likely)
        rates = system_rates()
        assert set(rates) == set(likely + unlikely)
        assert all(rates[fanhao] == RATE_VALUE.LIKE for fanhao in likely)
        assert sum(rates[fanhao] for fanhao in unlikely) == 10
        assert user_rates() == before
        assert '打分完成：给 40 部没有用户打标的影片打了分，推荐 20 部，不太像 20 部' in caplog.messages
        assert not [m for m in caplog.messages if m.startswith('跳过')]

    @pytest.mark.parametrize('total, expected', [(0, 0), (1, 1), (19, 19), (20, 20), (21, 20), (68, 20), (69, 21),
                                                 (100, 30), (284, 85), (285, 86), (30000, 9000)])
    def test_the_top_thirty_percent_but_at_least_twenty_are_recommended(self, total, expected):
        assert classifier.recommended_count(total) == expected

    def test_scoring_again_gives_the_same_split(self, movies):
        movies(120, like=True)
        movies(120, like=False)
        movies(10, like=True, rated=False)
        movies(30, like=False, rated=False)
        classifier.train()
        classifier.recommend()
        first = system_rates(), {fanhao: row.score for fanhao, row in stored_scores().items()}
        assert classifier.recommend() == (40, 20, 0)
        assert (system_rates(), {fanhao: row.score for fanhao, row in stored_scores().items()}) == first

    def test_nothing_to_score(self, movies, caplog):
        movies(120, like=True)
        movies(120, like=False)
        classifier.train()
        caplog.set_level(logging.INFO, logger='bustag')
        assert classifier.recommend() == (0, 0, 0)
        assert '没有需要打分的影片' in caplog.messages

    def test_movies_tagged_during_scoring_keep_the_user_tag(self, movies, monkeypatch, caplog):
        movies(120, like=True)
        movies(120, like=False)
        new = movies(10, like=True, rated=False)
        classifier.train()
        real_load = prepare.load_unlabeled

        def user_tags_after_loading():
            rows = real_load()
            ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE, new[3])
            ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, new[7])
            return rows

        monkeypatch.setattr(prepare, 'load_unlabeled', user_tags_after_loading)
        caplog.set_level(logging.INFO, logger='bustag')
        assert classifier.recommend() == (8, 8, 2)
        assert [(r.rate_type, r.rate_value) for r in map(ItemRate.get_by_fanhao, (new[3], new[7]))] == [
            (RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE), (RATE_TYPE.USER_RATE, RATE_VALUE.LIKE)]
        assert sorted(system_rates()) == sorted(set(new) - {new[3], new[7]})
        assert sorted(stored_scores()) == sorted(set(new) - {new[3], new[7]})
        assert '跳过 2 部（打分期间已经被打标或删除）' in caplog.messages

    def test_no_model_yet(self, movies):
        movies(3, like=True, rated=False)
        with pytest.raises(FileNotFoundError):
            classifier.load()
        with pytest.raises(FileNotFoundError):
            classifier.recommend()
        assert system_rates() == {} and stored_scores() == {}

    @pytest.mark.parametrize('content', [b'garbage', json.dumps({**VALID, 'tags': {}}).encode()])
    def test_unusable_model_file_changes_nothing(self, movies, data_dir, content):
        movies(120, like=True)
        movies(120, like=False)
        fanhaos = movies(3, like=True, rated=False)
        classifier.train()
        classifier.recommend()
        before = system_rates(), sorted(stored_scores())
        movies(2, like=False, rated=False)
        model_file(data_dir).write_bytes(content)
        with pytest.raises(ModelLoadError, match='重新训练'):
            classifier.recommend()
        assert (system_rates(), sorted(stored_scores())) == before
        assert sorted(before[1]) == sorted(fanhaos)


class TestTrainAndRecommend:
    def test_movies_are_scored_again_right_after_training(self, movies):
        movies(120, like=True)
        movies(120, like=False)
        likely = movies(10, like=True, rated=False)
        unlikely = movies(30, like=False, rated=False)
        # 上一个模型把它们全判成了喜欢
        for fanhao in likely + unlikely:
            ItemRate.saveit(RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE, fanhao)
        before = user_rates()
        saved, result = classifier.train_and_recommend()
        assert classifier.load() == saved
        assert result == (40, 20, 0)
        rates = system_rates()
        assert all(rates[fanhao] == RATE_VALUE.LIKE for fanhao in likely)
        assert sum(rates[fanhao] for fanhao in unlikely) == 10
        assert sorted(stored_scores()) == sorted(likely + unlikely)
        assert user_rates() == before

    def test_nothing_is_scored_when_training_fails(self, movies, write_model):
        movies(50, like=True)
        movies(5, like=True, rated=False)
        write_model()
        with pytest.raises(ValueError, match='训练数据不足'):
            classifier.train_and_recommend()
        assert system_rates() == {} and stored_scores() == {}

    def test_a_database_held_by_another_program_costs_the_scores_not_the_new_model(self, movies, locked_database,
                                                                                    caplog):
        movies(120, like=True)
        movies(120, like=False)
        unrated = movies(10, like=True, rated=False)
        ItemRate.saveit(RATE_TYPE.SYSTEM_RATE, RATE_VALUE.DISLIKE, unrated[0])
        before = user_rates()
        with locked_database():
            saved, result = classifier.train_and_recommend()
        # 模型已经保存；打分等不到写锁，这一次没有做成，不算训练失败
        assert result is None
        assert classifier.load() == saved
        assert system_rates() == {unrated[0]: RATE_VALUE.DISLIKE} and stored_scores() == {}
        assert user_rates() == before
        assert warning_messages(caplog) == [
            '模型已保存，但数据库正被别的程序占用（等了好一会儿还没轮到），影片还没有重新打分：下一次抓取后会自动打分']
        # 锁放开以后再打分就成了
        assert classifier.recommend() == (10, 10, 0)

    def test_other_database_errors_while_scoring_are_not_swallowed(self, movies, monkeypatch):
        movies(120, like=True)
        movies(120, like=False)

        def broken(model):
            raise OperationalError('no such table: item_score')

        monkeypatch.setattr(classifier, '_recommend', broken)
        with pytest.raises(OperationalError, match='no such table'):
            classifier.train_and_recommend()

    def test_another_scoring_cannot_slip_in_between_training_and_scoring(self, movies, monkeypatch):
        movies(120, like=True)
        movies(120, like=False)
        movies(10, like=True, rated=False)
        trained, release = threading.Event(), threading.Event()
        real_recommend = classifier._recommend
        order = []

        def slow_recommend(model):
            if not order:
                trained.set()
                release.wait(timeout=10)
            order.append(threading.current_thread().name)
            return real_recommend(model)

        monkeypatch.setattr(classifier, '_recommend', slow_recommend)

        def run(action):
            try:
                action()
            finally:
                db.db.close()

        first = threading.Thread(target=run, args=(classifier.train_and_recommend,), name='train')
        second = threading.Thread(target=run, args=(classifier.recommend,), name='recommend')
        first.start()
        try:
            assert trained.wait(timeout=60)
            second.start()
            second.join(timeout=0.5)
            assert second.is_alive()
        finally:
            release.set()
            first.join(timeout=60)
            if second.ident is not None:
                second.join(timeout=60)
        assert order == ['train', 'recommend']


class TestOldModelFiles:
    '''旧版本程序把模型存成 model.pkl 和 label_binarizer.pkl 两个 pickle 文件，新版本不读它们，只提示重新训练'''

    @pytest.mark.parametrize('names', [('model.pkl', 'label_binarizer.pkl'), ('model.pkl',), ('label_binarizer.pkl',)])
    def test_old_files_alone_need_retraining(self, movies, data_dir, names):
        fanhaos = movies(3, like=True, rated=False)
        for name in names:
            (data_dir / 'model' / name).write_bytes(b'left by an older version')
        for action in (classifier.load, classifier.recommend):
            with pytest.raises(LegacyModelError) as info:
                action()
            assert str(info.value) == persist.LEGACY_MODEL
        assert '旧版本程序训练的' in persist.LEGACY_MODEL and '重新训练' in persist.LEGACY_MODEL
        assert isinstance(info.value, ModelLoadError)
        assert all(ItemRate.get_by_fanhao(fanhao) is None for fanhao in fanhaos)

    def test_old_files_are_never_unpickled(self, data_dir):
        class Marker:
            def __reduce__(self):
                return exec, ('import builtins; builtins.bustag_old_model_was_loaded = True',)

        for name in ('model.pkl', 'label_binarizer.pkl'):
            (data_dir / 'model' / name).write_bytes(pickle.dumps(Marker()))
        with pytest.raises(LegacyModelError):
            classifier.load()
        assert not hasattr(builtins, 'bustag_old_model_was_loaded')

    def test_a_new_model_next_to_the_old_files_is_used(self, movies, data_dir):
        movies(120, like=True)
        movies(120, like=False)
        movies(5, like=True, rated=False)
        for name in ('model.pkl', 'label_binarizer.pkl'):
            (data_dir / 'model' / name).write_bytes(b'left by an older version')
        saved = classifier.train()
        assert classifier.load() == saved
        assert classifier.recommend() == (5, 5, 0)
        # 旧文件是用户数据目录里的东西，留着不动
        assert model_folder(data_dir) == ['label_binarizer.pkl', 'model.json', 'model.pkl']


class TestSaveModel:
    '''保存失败时原来的模型文件保持原样、仍能打分，也不留下临时文件'''

    @pytest.fixture
    def saved(self, movies, make_item, data_dir):
        '''训练并保存一次，返回模型文件的内容；之后再训练时文件会和原来不同'''
        movies(120, like=True)
        movies(120, like=False)
        movies(10, like=True, rated=False)
        classifier.train()
        make_item('NEW-001', tags=[('genre', '新类别')])
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, 'NEW-001')
        return model_file(data_dir).read_bytes()

    def assert_kept(self, data_dir, before):
        assert model_file(data_dir).read_bytes() == before
        assert model_folder(data_dir) == ['model.json']
        assert classifier.recommend().scored == 10

    @staticmethod
    def fail_replacing_model(monkeypatch, error):
        '''替换 model.json 时出错（error(第几次) 返回异常就抛，返回 None 就照常替换）'''
        real_replace = os.replace
        attempts = []

        def replace(source, target):
            if os.path.basename(target) == 'model.json':
                attempts.append(target)
                failure = error(len(attempts))
                if failure:
                    raise failure
            real_replace(source, target)

        monkeypatch.setattr(util.os, 'replace', replace)
        monkeypatch.setattr(util, 'REPLACE_RETRY_DELAYS', (0,) * len(util.REPLACE_RETRY_DELAYS))
        return attempts

    def test_disk_full_while_writing(self, saved, data_dir, monkeypatch):
        real_write_temp = util.write_temp_file

        def write_temp(path, write):
            def half_then_full(f):
                f.write(b'{"version": 1, "tags"')
                raise OSError(errno.ENOSPC, '磁盘空间不足')
            return real_write_temp(path, half_then_full)

        monkeypatch.setattr(util, 'write_temp_file', write_temp)
        with pytest.raises(ModelSaveError, match=r'^无法保存模型文件到 .+model（磁盘空间不足），原来的模型没有改动$'):
            classifier.train()
        self.assert_kept(data_dir, saved)

    def test_failure_replacing_the_file(self, saved, data_dir, monkeypatch):
        self.fail_replacing_model(monkeypatch, lambda attempt: OSError(errno.EIO, '读写错误'))
        with pytest.raises(ModelSaveError, match=r'（读写错误），原来的模型没有改动$'):
            classifier.train()
        self.assert_kept(data_dir, saved)

    def test_first_save_failing_leaves_nothing_behind(self, movies, data_dir, monkeypatch):
        movies(120, like=True)
        movies(120, like=False)
        self.fail_replacing_model(monkeypatch, lambda attempt: OSError(errno.EIO, '读写错误'))
        with pytest.raises(ModelSaveError):
            classifier.train()
        assert model_folder(data_dir) == []
        with pytest.raises(FileNotFoundError):
            classifier.load()

    def test_a_read_never_meets_the_model_file_while_it_is_being_replaced(self, write_model, monkeypatch):
        # Windows 上正在被替换的那一瞬间去打开文件会被拒绝（拒绝访问），读的一方就会把刚训练好的模型当成读不了。
        # 所以读和"替换的那一下"不同时进行：替换开始以后才来读的，等它换完，读到新的那一份
        path = str(write_model(tags={'g1': (1.0, 1.0)}))
        replacing, finish = threading.Event(), threading.Event()
        real_replace = os.replace
        read = []

        def slow_replace(source, target):
            replacing.set()
            assert finish.wait(timeout=10)
            real_replace(source, target)

        monkeypatch.setattr(util.os, 'replace', slow_replace)
        writer = threading.Thread(target=persist.save_model, args=(path, model_with({'g2': (1.0, 2.0)})))
        reader = threading.Thread(target=lambda: read.append(persist.read_model(path)))
        writer.start()
        try:
            assert replacing.wait(timeout=10)
            reader.start()
            reader.join(timeout=0.5)
            assert reader.is_alive() and read == []
        finally:
            finish.set()
            writer.join(timeout=10)
            reader.join(timeout=10)
        assert [model.tags for model in read] == [{'g2': (1.0, 2.0)}]

    def test_reading_does_not_wait_for_a_save_that_is_stuck(self, write_model, monkeypatch):
        path = str(write_model(tags={'g1': (1.0, 1.0)}))
        waiting, release = threading.Event(), threading.Event()
        outcome = []

        def refused(source, target):
            raise PermissionError(errno.EACCES, '另一个程序正在使用此文件')

        def wait_before_trying_again(seconds):
            waiting.set()
            release.wait(timeout=10)

        def save():
            try:
                persist.save_model(path, model_with({'g2': (1.0, 2.0)}))
            except ModelSaveError as ex:
                outcome.append(str(ex))

        monkeypatch.setattr(util.os, 'replace', refused)
        monkeypatch.setattr(util.time, 'sleep', wait_before_trying_again)
        writer = threading.Thread(target=save)
        writer.start()
        try:
            assert waiting.wait(timeout=10)
            # 保存的一方正等着再试：别的页面这时读模型不用等它，读到的是原来的那份
            reader = threading.Thread(target=lambda: outcome.append(persist.read_model(path).tags))
            reader.start()
            reader.join(timeout=5)
            assert outcome == [{'g1': (1.0, 1.0)}]
        finally:
            release.set()
            writer.join(timeout=10)
        assert len(outcome) == 2 and '另一个程序正在使用此文件' in outcome[1]

    def test_file_in_use_is_retried(self, saved, data_dir, monkeypatch):
        attempts = self.fail_replacing_model(
            monkeypatch, lambda attempt: PermissionError(errno.EACCES, '文件正在使用') if attempt <= 2 else None)
        trained = classifier.train()
        assert len(attempts) == 3
        assert model_file(data_dir).read_bytes() != saved
        assert classifier.load() == trained
        assert model_folder(data_dir) == ['model.json']

    def test_file_in_use_for_too_long(self, saved, data_dir, monkeypatch):
        attempts = self.fail_replacing_model(monkeypatch, lambda attempt: PermissionError(errno.EACCES, '文件正在使用'))
        with pytest.raises(ModelSaveError, match=r'（文件正在使用），原来的模型没有改动$'):
            classifier.train()
        assert len(attempts) == len(util.REPLACE_RETRY_DELAYS) + 1
        self.assert_kept(data_dir, saved)

    def test_model_file_being_read_meanwhile(self, saved, data_dir, monkeypatch):
        '''Windows 上别人正读着 model.json 时不能替换它，等一会儿、读完了再替换；别的系统直接替换'''
        reader = open(model_file(data_dir), 'rb')
        waits = []

        def sleep(seconds):
            waits.append(seconds)
            reader.close()

        monkeypatch.setattr(util, 'time', types.SimpleNamespace(sleep=sleep))
        try:
            classifier.train()
        finally:
            reader.close()
        assert model_file(data_dir).read_bytes() != saved
        assert model_folder(data_dir) == ['model.json']
        if sys.platform == 'win32':
            assert waits == [util.REPLACE_RETRY_DELAYS[0]]


class TestModelLock:
    def test_scoring_waits_for_training(self, movies, monkeypatch):
        movies(120, like=True)
        movies(120, like=False)
        movies(10, like=True, rated=False)
        classifier.train()
        saving, release = threading.Event(), threading.Event()
        real_save = classifier.save_model

        def slow_save(*args):
            saving.set()
            release.wait(timeout=10)
            real_save(*args)

        monkeypatch.setattr(classifier, 'save_model', slow_save)
        results = []

        def run(action):
            try:
                results.append(action())
            finally:
                db.db.close()

        trainer = threading.Thread(target=run, args=(classifier.train,))
        scorer = threading.Thread(target=run, args=(classifier.recommend,))
        trainer.start()
        try:
            assert saving.wait(timeout=30)
            scorer.start()
            scorer.join(timeout=0.5)
            # 训练还没保存完：打分在等，没有去读马上要被替换的文件，也没有写系统评分
            assert scorer.is_alive()
            assert system_rates() == {}
        finally:
            release.set()
            trainer.join(timeout=60)
            if scorer.ident is not None:
                scorer.join(timeout=60)
        assert [type(result).__name__ for result in results] == ['SavedModel', 'ScoreResult']
        assert results[1] == (10, 10, 0)
        assert len(system_rates()) == 10


class TestModelFile:
    def test_a_valid_file_loads(self, data_dir, caplog):
        write_content(data_dir, VALID)
        assert classifier.load() == SavedModel(
            intercept=0.5, tags={'g1': (1.5, 0.25), 's1': (2.0, -0.75)}, trained_at='2026-11-06 12:00:00',
            report=Report(likes=150, dislikes=60, folds=5, repeats=10, like_recall=0.9, dislike_recall=0.8, auc=0.95))
        assert warning_messages(caplog) == []

    def test_whole_numbers_are_accepted_where_fractions_are_expected(self, data_dir):
        write_content(data_dir, {**VALID, 'intercept': 1, 'tags': {'g1': [2, 0]},
                                 'report': {**VALID['report'], 'like_recall': 1, 'auc': 1}})
        saved = classifier.load()
        assert (saved.intercept, saved.tags, saved.report.like_recall) == (1, {'g1': (2, 0)}, 1)
        # 读进来都是小数：之后的计算不会因为整数太大而出错
        numbers = [saved.intercept, *saved.tags['g1'], saved.report.like_recall, saved.report.auc]
        assert all(type(number) is float for number in numbers)

    def test_no_file_means_no_model(self, data_dir):
        with pytest.raises(FileNotFoundError):
            classifier.load()

    @pytest.mark.parametrize('content', [b'not json at all', b'', b'\xff\xfe\x00{', b'{"version": 1, "tags": {',
                                         json.dumps(VALID).encode('utf-16')])
    def test_file_that_cannot_be_read(self, data_dir, content):
        model_file(data_dir).write_bytes(content)
        with pytest.raises(ModelLoadError) as info:
            classifier.load()
        assert str(info.value) == ('模型文件 model.json 无法读取（不是有效的 JSON，文件可能已经损坏）。'
                                   '请到网页的"模型"页点击"开始训练"重新训练')
        assert not isinstance(info.value, LegacyModelError)

    @pytest.mark.parametrize('content', [
        [1, 2, 3],
        'model',
        {key: value for key, value in VALID.items() if key != 'version'},
        {**VALID, 'version': 2},
        {**VALID, 'version': '1'},
        {**VALID, 'version': True},
        {key: value for key, value in VALID.items() if key != 'intercept'},
        {**VALID, 'intercept': '0.5'},
        {**VALID, 'intercept': True},
        {**VALID, 'intercept': float('nan')},
        {**VALID, 'intercept': float('inf')},
        {key: value for key, value in VALID.items() if key != 'tags'},
        {**VALID, 'tags': {}},
        {**VALID, 'tags': [['g1', 1.5, 0.25]]},
        {**VALID, 'tags': {'g1': 1.5}},
        {**VALID, 'tags': {'g1': [1.5]}},
        {**VALID, 'tags': {'g1': [1.5, 0.25, 3]}},
        {**VALID, 'tags': {'g1': [1.5, 'x']}},
        {**VALID, 'tags': {'g1': [0, 0.25]}},
        {**VALID, 'tags': {'g1': [-1.5, 0.25]}},
        {**VALID, 'tags': {'g1': [float('nan'), 0.25]}},
        {**VALID, 'tags': {'g1': [1.5, float('-inf')]}},
        {key: value for key, value in VALID.items() if key != 'report'},
        {**VALID, 'report': 'good'},
        {**VALID, 'report': {key: value for key, value in VALID['report'].items() if key != 'auc'}},
        {**VALID, 'report': {**VALID['report'], 'likes': 0}},
        {**VALID, 'report': {**VALID['report'], 'dislikes': 0}},
        {**VALID, 'report': {**VALID['report'], 'likes': -1}},
        {**VALID, 'report': {**VALID['report'], 'dislikes': 1.5}},
        {**VALID, 'report': {**VALID['report'], 'folds': 0}},
        {**VALID, 'report': {**VALID['report'], 'repeats': 0}},
        {**VALID, 'report': {**VALID['report'], 'like_recall': 1.2}},
        {**VALID, 'report': {**VALID['report'], 'dislike_recall': -0.1}},
        {**VALID, 'report': {**VALID['report'], 'auc': 'high'}},
        {**VALID, 'report': {**VALID['report'], 'auc': float('nan')}},
        {key: value for key, value in VALID.items() if key != 'trained_at'},
        {**VALID, 'trained_at': 20261106},
        # 大到换不成小数的整数，和大得不像话的数
        {**VALID, 'intercept': 10 ** 400},
        {**VALID, 'intercept': -(10 ** 400)},
        {**VALID, 'intercept': 1e300},
        {**VALID, 'tags': {'g1': [10 ** 400, 0.25]}},
        {**VALID, 'tags': {'g1': [1.5, 10 ** 400]}},
        {**VALID, 'tags': {'g1': [1e200, 0.25]}},
        {**VALID, 'tags': {'g1': [1.5, -1e200]}},
        {**VALID, 'report': {**VALID['report'], 'auc': 10 ** 400}},
        {**VALID, 'report': {**VALID['report'], 'likes': 10 ** 400}},
        {**VALID, 'report': {**VALID['report'], 'dislikes': 10 ** 400}},
        {**VALID, 'report': {**VALID['report'], 'folds': 10 ** 30}},
        {**VALID, 'report': {**VALID['report'], 'repeats': 10 ** 30}},
    ])
    def test_file_with_the_wrong_content(self, data_dir, content):
        write_content(data_dir, content)
        with pytest.raises(ModelLoadError) as info:
            classifier.load()
        assert str(info.value) == '模型文件 model.json 格式不对，请到网页的"模型"页点击"开始训练"重新训练'

    def test_file_far_larger_than_any_model_is_not_read_in(self, data_dir, monkeypatch):
        write_content(data_dir, VALID)
        classifier.load()
        monkeypatch.setattr(persist, 'MAX_FILE_SIZE', model_file(data_dir).stat().st_size - 1)
        with pytest.raises(ModelLoadError, match='模型文件 model.json 格式不对'):
            classifier.load()

    def test_file_that_cannot_be_opened(self, data_dir, monkeypatch):
        write_content(data_dir, VALID)
        real_open = builtins.open

        def locked(path, *args, **kwargs):
            if os.path.basename(str(path)) == 'model.json':
                raise PermissionError(errno.EACCES, '拒绝访问')
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, 'open', locked)
        with pytest.raises(ModelLoadError) as info:
            classifier.load()
        assert str(info.value) == '模型文件 model.json 无法读取（拒绝访问）。请到网页的"模型"页点击"开始训练"重新训练'

    def test_the_reason_a_file_cannot_be_opened_is_given_in_chinese(self, data_dir):
        # 文件名被一个文件夹占着：Windows 报 Permission denied，别的系统报 Is a directory，都是英文
        model_file(data_dir).mkdir()
        with pytest.raises(ModelLoadError) as info:
            classifier.load()
        assert re.fullmatch(r'模型文件 model\.json 无法读取（[^A-Za-z（）]+）。请到网页的"模型"页点击"开始训练"重新训练',
                            str(info.value))

    def test_the_reason_a_model_cannot_be_saved_is_given_in_chinese(self, data_dir, monkeypatch):
        def read_only(path, write):
            raise PermissionError(errno.EACCES, 'Permission denied')

        monkeypatch.setattr(util, 'write_temp_file', read_only)
        with pytest.raises(ModelSaveError) as info:
            persist.save_model(str(model_file(data_dir)), model_with({'g1': (1.0, 1.0)}))
        assert str(info.value).endswith('（拒绝访问：没有权限，或者正被别的程序占用），原来的模型没有改动')

    def test_saving_then_reading_gives_the_same_model(self, data_dir):
        model = model_with({'标签一': (1.25, -0.5), '<b>"引号"</b>': (3.0, 0.1), '😀 表情': (2.0, 2.0)}, intercept=-1.5)
        persist.save_model(str(model_file(data_dir)), model)
        assert persist.read_model(str(model_file(data_dir))) == model
        text = model_file(data_dir).read_text(encoding='utf-8')
        assert '标签一' in text and '😀 表情' in text
