'''
模型：用合成的打标数据训练和推荐（"喜欢"的影片都带 g0 和 s0 两个标签，两类可以完全分开），
以及数据不足、标签单一、模型文件读不了等情况
'''
import logging
import pickle
import random
import sys
import types
import warnings

import pytest
import sklearn
from sklearn.metrics import confusion_matrix
from sklearn.neighbors import KNeighborsClassifier
from sklearn.preprocessing import MultiLabelBinarizer

from bustag.model import classifier, prepare
from bustag.model.persist import ModelLoadError, dump_model
from bustag.spider import db
from bustag.spider.db import RATE_TYPE, RATE_VALUE, ItemRate

GENRES = [f'g{i}' for i in range(1, 20)]
STARS = [f's{i}' for i in range(1, 30)]
SCORES = {'precision': 0.5, 'recall': 0.5, 'f1': 0.5}
SKLEARN_WARNINGS = ('column-vector', 'ill-defined', 'zero_division', 'unknown class')


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
    return data_dir / classifier.MODEL_FILE


def binarizer_file(data_dir):
    return data_dir / prepare.BINARIZER_PATH


def warning_messages(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


class TestTrain:
    def test_train_saves_model_with_version(self, movies, data_dir):
        movies(120, like=True)
        movies(120, like=False)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            model, scores = classifier.train()
        assert set(scores) == {'precision', 'recall', 'f1'}
        assert all(0 <= value <= 1 for value in scores.values())
        # 两类完全可分，正常是 1.0；阈值留出余量，容许不同 scikit-learn 版本处理近邻并列时有出入
        assert scores['f1'] >= 0.9
        assert not [w for w in caught if any(key in str(w.message) for key in SKLEARN_WARNINGS)]
        saved = pickle.loads(model_file(data_dir).read_bytes())
        assert set(saved) == {'model', 'scores', 'sklearn_version'}
        assert saved['sklearn_version'] == sklearn.__version__
        assert saved['scores'] == scores
        assert binarizer_file(data_dir).is_file()

    def test_not_enough_data(self, movies, data_dir):
        movies(50, like=True)
        movies(50, like=False)
        with pytest.raises(ValueError, match=r'^训练数据不足, 无法训练模型\. 需要200, 当前100$'):
            classifier.train()
        assert not binarizer_file(data_dir).exists()
        assert not model_file(data_dir).exists()

    @pytest.mark.parametrize('likes, dislikes, message', [
        (210, 0, '打标数据里只有"喜欢"一种标签'),
        (0, 210, '打标数据里只有"不喜欢"一种标签'),
        (209, 1, '"喜欢"和"不喜欢"都至少需要 2 条打标数据'),
    ])
    def test_labels_must_include_both_classes(self, movies, data_dir, likes, dislikes, message):
        movies(likes, like=True)
        movies(dislikes, like=False)
        with pytest.raises(ValueError, match=message):
            classifier.train()
        assert not binarizer_file(data_dir).exists()

    def test_no_rated_data(self):
        with pytest.raises(ValueError, match='还没有打标数据'):
            prepare.prepare_data()

    def test_evaluate_without_positive_predictions_has_no_warnings(self):
        y_test, y_pred = [0, 1, 0, 1], [0, 0, 0, 0]
        with warnings.catch_warnings():
            warnings.simplefilter('error')
            scores = classifier.evaluate(confusion_matrix(y_test, y_pred, labels=[0, 1]), y_test, y_pred)
        assert scores == {'precision': 0.0, 'recall': 0.0, 'f1': 0.0}

    def test_evaluate_rounds_to_two_digits(self):
        y_test, y_pred = [0, 1, 1, 0], [0, 1, 0, 0]
        scores = classifier.evaluate(confusion_matrix(y_test, y_pred, labels=[0, 1]), y_test, y_pred)
        assert scores == {'precision': 1.0, 'recall': 0.5, 'f1': 0.67}


class TestRecommend:
    def test_recommend_writes_system_rates(self, movies, caplog):
        movies(120, like=True)
        movies(120, like=False)
        likely = movies(10, like=True, rated=False)
        unlikely = movies(10, like=False, rated=False)
        _, scores = classifier.train()
        caplog.clear()
        assert classifier.load()[1] == scores
        assert warning_messages(caplog) == []

        total, count = classifier.recommend()
        assert total == 20
        rates = {fanhao: ItemRate.get_by_fanhao(fanhao) for fanhao in likely + unlikely}
        assert all(r.rate_type == RATE_TYPE.SYSTEM_RATE for r in rates.values())
        assert count == sum(r.rate_value for r in rates.values())
        correct = sum(rates[f].rate_value == 1 for f in likely) + sum(rates[f].rate_value == 0 for f in unlikely)
        assert correct >= 18
        assert all(r.rete_time is not None for r in rates.values())
        assert db.get_today_recommend_count() == count
        assert f'推荐完成：预测 20 部，推荐 {count} 部' in caplog.messages
        assert not [m for m in caplog.messages if m.startswith('跳过')]

        caplog.clear()
        assert classifier.recommend() == (0, 0)
        assert '没有需要推荐的新影片' in caplog.messages

    def test_movies_tagged_during_recommendation_keep_the_user_tag(self, movies, monkeypatch, caplog):
        movies(120, like=True)
        movies(120, like=False)
        new = movies(10, like=True, rated=False)
        classifier.train()
        real_prepare = classifier.prepare_predict_data

        def user_tags_after_prepare():
            ids, X = real_prepare()
            ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE, new[3])
            ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, new[7])
            return ids, X

        monkeypatch.setattr(classifier, 'prepare_predict_data', user_tags_after_prepare)
        caplog.clear()
        total, count = classifier.recommend()
        assert total == 10
        assert [(r.rate_type, r.rate_value) for r in map(ItemRate.get_by_fanhao, (new[3], new[7]))] == [
            (RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE), (RATE_TYPE.USER_RATE, RATE_VALUE.LIKE)]
        system = ItemRate.select().where(ItemRate.rate_type == RATE_TYPE.SYSTEM_RATE)
        assert sorted(r.item_id for r in system) == sorted(set(new) - {new[3], new[7]})
        assert f'推荐完成：预测 10 部，推荐 {count} 部' in caplog.messages
        assert '跳过 2 部（推荐期间已被打标）' in caplog.messages

    def test_many_new_movies_are_written_in_batches(self, movies, monkeypatch):
        movies(120, like=True)
        movies(120, like=False)
        new = movies(25, like=False, rated=False)
        classifier.train()
        monkeypatch.setattr(classifier, 'INSERT_BATCH', 10)
        assert classifier.recommend()[0] == 25
        assert sorted(r.item_id for r in ItemRate.select().where(ItemRate.rate_type == RATE_TYPE.SYSTEM_RATE)) == new

    def test_unknown_tags_are_ignored_quietly(self, movies, make_item, caplog):
        movies(120, like=True)
        movies(120, like=False)
        classifier.train()
        make_item('NEW-001', tags=[('genre', 'g1'), ('genre', '新类别'), ('star', '新演员A'), ('star', '新演员B')])
        caplog.set_level(logging.DEBUG, logger='bustag')
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            ids, X = prepare.prepare_predict_data()
        assert ids == ['NEW-001']
        assert X.sum() == 1
        assert [str(w.message) for w in caught if 'unknown class' in str(w.message)] == []
        assert '本次有 3 个训练时没见过的标签，已忽略' in caplog.messages

    def test_no_model_yet(self, movies):
        movies(3, like=True, rated=False)
        with pytest.raises(FileNotFoundError):
            classifier.load()
        with pytest.raises(FileNotFoundError):
            classifier.recommend()

    @pytest.mark.parametrize('broken, content', [
        ('model', b'garbage'),
        ('model', pickle.dumps((MultiLabelBinarizer(), SCORES))),
        ('binarizer', b'garbage'),
        ('binarizer', pickle.dumps(KNeighborsClassifier())),
    ])
    def test_unusable_files_stop_recommendation(self, movies, data_dir, broken, content):
        fanhaos = movies(3, like=True, rated=False)
        binarizer = MultiLabelBinarizer().fit([GENRES + STARS + ['g0', 's0']])
        dump_model(str(binarizer_file(data_dir)), binarizer)
        dump_model(str(model_file(data_dir)), {'model': KNeighborsClassifier(), 'scores': SCORES,
                                               'sklearn_version': sklearn.__version__})
        target = model_file(data_dir) if broken == 'model' else binarizer_file(data_dir)
        target.write_bytes(content)
        with pytest.raises(ModelLoadError, match='重新训练'):
            classifier.recommend()
        assert all(ItemRate.get_by_fanhao(f) is None for f in fanhaos)


class TestModelFile:
    def save(self, data_dir, content):
        model_file(data_dir).write_bytes(pickle.dumps(content))
        binarizer_file(data_dir).write_bytes(pickle.dumps(MultiLabelBinarizer().fit([GENRES + STARS])))

    @pytest.mark.parametrize('content, detail', [
        (b'not a pickle at all', '模型文件 label_binarizer.pkl 无法读取'),
        (pickle.dumps(KNeighborsClassifier()), '模型文件 label_binarizer.pkl 格式不对'),
        (pickle.dumps(MultiLabelBinarizer()), '模型文件 label_binarizer.pkl 格式不对'),
    ], ids=['unreadable', 'not-a-binarizer', 'never-fitted'])
    def test_binarizer_is_checked_too(self, data_dir, content, detail):
        self.save(data_dir, {'model': KNeighborsClassifier(), 'scores': SCORES,
                             'sklearn_version': sklearn.__version__})
        binarizer_file(data_dir).write_bytes(content)
        with pytest.raises(ModelLoadError, match=detail) as info:
            classifier.load()
        assert '重新训练' in str(info.value)

    def test_missing_binarizer_means_no_model(self, data_dir):
        self.save(data_dir, {'model': KNeighborsClassifier(), 'scores': SCORES,
                             'sklearn_version': sklearn.__version__})
        binarizer_file(data_dir).unlink()
        with pytest.raises(FileNotFoundError):
            classifier.load()

    def test_current_format_loads_quietly(self, data_dir, caplog):
        self.save(data_dir, {'model': KNeighborsClassifier(), 'scores': SCORES,
                             'sklearn_version': sklearn.__version__})
        model, scores = classifier.load()
        assert isinstance(model, KNeighborsClassifier)
        assert scores == SCORES
        assert warning_messages(caplog) == []

    def test_old_tuple_format_loads_with_a_hint(self, data_dir, caplog):
        self.save(data_dir, (KNeighborsClassifier(), SCORES))
        assert classifier.load()[1] == SCORES
        assert any('旧版本程序保存' in m and '建议' in m for m in warning_messages(caplog))

    def test_other_sklearn_version_loads_with_a_hint(self, data_dir, caplog):
        self.save(data_dir, {'model': KNeighborsClassifier(), 'scores': SCORES, 'sklearn_version': '0.21.3'})
        assert classifier.load()[1] == SCORES
        assert any('0.21.3' in m and sklearn.__version__ in m and '建议' in m
                   for m in warning_messages(caplog))

    @pytest.mark.parametrize('content, detail', [
        (b'not a pickle at all', '无法读取'),
        (b'', '无法读取'),
        (pickle.dumps([1, 2, 3]), '格式不对'),
        (pickle.dumps({'model': 1}), '格式不对'),
        (pickle.dumps({'model': KNeighborsClassifier()}), '格式不对'),
        (pickle.dumps({'model': KNeighborsClassifier(), 'scores': {'f1': 0.5}}), '格式不对'),
        (pickle.dumps({'model': KNeighborsClassifier(), 'scores': 'high'}), '格式不对'),
        (pickle.dumps((MultiLabelBinarizer(), SCORES)), '格式不对'),
        (pickle.dumps((KNeighborsClassifier(), {'precision': '0.5', 'recall': 0.5, 'f1': 0.5})), '格式不对'),
        (pickle.dumps(MultiLabelBinarizer()), '格式不对'),
    ])
    def test_unreadable_model_file(self, data_dir, content, detail):
        model_file(data_dir).write_bytes(content)
        with pytest.raises(ModelLoadError, match=detail) as info:
            classifier.load()
        assert '重新训练' in str(info.value)

    def test_model_pickled_with_a_class_that_no_longer_exists(self, data_dir):
        module = types.ModuleType('bustag_removed_module')

        class Removed:
            pass

        Removed.__module__ = module.__name__
        Removed.__qualname__ = 'Removed'
        module.Removed = Removed
        sys.modules[module.__name__] = module
        try:
            content = pickle.dumps((Removed(), SCORES))
        finally:
            del sys.modules[module.__name__]
        model_file(data_dir).write_bytes(content)
        with pytest.raises(ModelLoadError, match='ModuleNotFoundError') as info:
            classifier.load()
        assert '多半是 scikit-learn 版本升级造成的' in str(info.value)
