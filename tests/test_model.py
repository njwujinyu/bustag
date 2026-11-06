'''
模型：用合成的打标数据训练和推荐（"喜欢"的影片都带 g0 和 s0 两个标签，两类可以完全分开），
以及数据不足、标签单一、模型文件读不了、模型和标签编码器对不上、保存失败、训练与推荐同时进行等情况
'''
import errno
import logging
import os
import pickle
import random
import sys
import threading
import types
import warnings

import pytest
import sklearn
from sklearn.metrics import confusion_matrix
from sklearn.neighbors import KNeighborsClassifier
from sklearn.preprocessing import MultiLabelBinarizer

from bustag.model import classifier, persist, prepare
from bustag.model.persist import PREDICT_ERROR, ModelLoadError, ModelSaveError
from bustag.spider import db
from bustag.spider.db import RATE_TYPE, RATE_VALUE, ItemRate

GENRES = [f'g{i}' for i in range(1, 20)]
STARS = [f's{i}' for i in range(1, 30)]
SCORES = {'precision': 0.5, 'recall': 0.5, 'f1': 0.5}
SKLEARN_WARNINGS = ('column-vector', 'ill-defined', 'zero_division', 'unknown class')


def usable_pair(classes):
    '''按 classes 训练的标签编码器，和用它编码的两部影片训练过的模型（两个能配合使用）'''
    binarizer = MultiLabelBinarizer().fit([classes])
    model = KNeighborsClassifier(n_neighbors=1).fit(binarizer.transform([classes[:1], classes[1:2]]), [0, 1])
    return binarizer, model


BINARIZER, MODEL = usable_pair(GENRES + STARS)


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
    return data_dir / persist.BINARIZER_PATH


def warning_messages(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def model_folder(data_dir):
    '''model/ 目录里的文件名（检查有没有留下临时文件）'''
    return sorted(path.name for path in (data_dir / 'model').iterdir())


def system_rated():
    return ItemRate.select().where(ItemRate.rate_type == RATE_TYPE.SYSTEM_RATE).count()


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
        assert model_folder(data_dir) == ['label_binarizer.pkl', 'model.pkl']

    def test_failure_after_fitting_keeps_the_saved_pair(self, movies, make_item, data_dir, monkeypatch):
        movies(120, like=True)
        movies(120, like=False)
        classifier.train()
        before = (model_file(data_dir).read_bytes(), binarizer_file(data_dir).read_bytes())
        # 新打标的影片带着新标签：要是再训练时提前写了标签编码器，它一定和原来的不同
        make_item('NEW-001', tags=[('genre', '新类别')])
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, 'NEW-001')

        def broken_evaluate(*args):
            raise RuntimeError('评估出错')

        monkeypatch.setattr(classifier, 'evaluate', broken_evaluate)
        with pytest.raises(RuntimeError, match='评估出错'):
            classifier.train()
        assert (model_file(data_dir).read_bytes(), binarizer_file(data_dir).read_bytes()) == before
        assert model_folder(data_dir) == ['label_binarizer.pkl', 'model.pkl']

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

        def user_tags_after_prepare(binarizer):
            ids, X = real_prepare(binarizer)
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

    def test_unknown_tags_are_ignored_quietly(self, movies, make_item, data_dir, caplog):
        movies(120, like=True)
        movies(120, like=False)
        classifier.train()
        make_item('NEW-001', tags=[('genre', 'g1'), ('genre', '新类别'), ('star', '新演员A'), ('star', '新演员B')])
        binarizer = persist.read_binarizer(str(binarizer_file(data_dir)))
        caplog.set_level(logging.DEBUG, logger='bustag')
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            ids, X = prepare.prepare_predict_data(binarizer)
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
        binarizer, model = usable_pair(GENRES + STARS + ['g0', 's0'])
        binarizer_file(data_dir).write_bytes(pickle.dumps(binarizer))
        model_file(data_dir).write_bytes(pickle.dumps({'model': model, 'scores': SCORES,
                                                       'sklearn_version': sklearn.__version__}))
        target = model_file(data_dir) if broken == 'model' else binarizer_file(data_dir)
        target.write_bytes(content)
        with pytest.raises(ModelLoadError, match='重新训练'):
            classifier.recommend()
        assert all(ItemRate.get_by_fanhao(f) is None for f in fanhaos)


class TestUnusablePair:
    '''
    两个文件都能读出来，但不能一起用来预测：模型页（load）和推荐都给同一个中文提示，不写系统评分
    '''

    @pytest.fixture
    def unrated(self, movies):
        movies(120, like=True)
        movies(120, like=False)
        fanhaos = movies(10, like=True, rated=False)
        classifier.train()
        return fanhaos

    @staticmethod
    def change_model(data_dir, change):
        saved = pickle.loads(model_file(data_dir).read_bytes())
        change(saved['model'])
        model_file(data_dir).write_bytes(pickle.dumps(saved))

    @staticmethod
    def assert_unusable(fanhaos):
        for action in (classifier.load, classifier.recommend):
            with pytest.raises(ModelLoadError) as info:
                action()
            assert str(info.value) == PREDICT_ERROR
        assert all(ItemRate.get_by_fanhao(f) is None for f in fanhaos)

    def test_binarizer_from_another_training(self, unrated, data_dir):
        binarizer_file(data_dir).write_bytes(pickle.dumps(MultiLabelBinarizer().fit([['x', 'y', 'z']])))
        self.assert_unusable(unrated)

    def test_model_that_fails_to_predict(self, unrated, data_dir):
        # 换了 scikit-learn 版本后常见：模型能读出来，但内部属性对不上，预测时出错
        self.change_model(data_dir, lambda model: delattr(model, 'n_samples_fit_'))
        self.assert_unusable(unrated)

    def test_mismatch_without_feature_count_is_found_by_trial_prediction(self, unrated, data_dir):
        self.change_model(data_dir, lambda model: delattr(model, 'n_features_in_'))
        binarizer_file(data_dir).write_bytes(pickle.dumps(MultiLabelBinarizer().fit([['x', 'y', 'z']])))
        self.assert_unusable(unrated)

    def test_encoding_the_new_movies_fails(self, unrated, monkeypatch):
        real_transform = MultiLabelBinarizer.transform

        def transform(self, y):
            if any(y):
                raise TypeError('标签编码出错')
            return real_transform(self, y)

        monkeypatch.setattr(MultiLabelBinarizer, 'transform', transform)
        classifier.load()
        with pytest.raises(ModelLoadError) as info:
            classifier.recommend()
        assert str(info.value) == PREDICT_ERROR
        assert system_rated() == 0

    def test_predicting_the_new_movies_fails(self, unrated, monkeypatch, caplog):
        real_predict = KNeighborsClassifier.predict

        def predict(self, X):
            if X.shape[0] > 1:
                raise ValueError('预测出错的细节')
            return real_predict(self, X)

        monkeypatch.setattr(KNeighborsClassifier, 'predict', predict)
        caplog.set_level(logging.DEBUG, logger='bustag')
        with pytest.raises(ModelLoadError) as info:
            classifier.recommend()
        assert str(info.value) == PREDICT_ERROR
        assert system_rated() == 0
        assert '预测出错：ValueError: 预测出错的细节' in caplog.messages


class TestSaveModelFiles:
    '''保存失败时原来的一对模型文件保持原样、仍能推荐，也不留下临时文件'''

    @pytest.fixture
    def saved(self, movies, make_item, data_dir):
        '''训练并保存一次，返回两个文件的内容；之后再训练时两个文件都会和原来不同'''
        movies(120, like=True)
        movies(120, like=False)
        movies(10, like=True, rated=False)
        classifier.train()
        make_item('NEW-001', tags=[('genre', '新类别')])
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.LIKE, 'NEW-001')
        return self.files(data_dir)

    @staticmethod
    def files(data_dir):
        return model_file(data_dir).read_bytes(), binarizer_file(data_dir).read_bytes()

    def assert_kept(self, data_dir, before):
        assert self.files(data_dir) == before
        assert model_folder(data_dir) == ['label_binarizer.pkl', 'model.pkl']
        assert classifier.recommend()[0] == 10

    @staticmethod
    def fail_replacing_model(monkeypatch, error):
        '''替换 model.pkl 时出错，替换别的文件照常'''
        real_replace = os.replace
        attempts = []

        def replace(source, target):
            if os.path.basename(target) == 'model.pkl':
                attempts.append(target)
                failure = error(len(attempts))
                if failure:
                    raise failure
            real_replace(source, target)

        monkeypatch.setattr(persist.os, 'replace', replace)
        monkeypatch.setattr(persist, 'REPLACE_RETRY_DELAYS', (0,) * len(persist.REPLACE_RETRY_DELAYS))
        return attempts

    def test_disk_full_while_writing(self, saved, data_dir, monkeypatch):
        real_write_temp = persist._write_temp

        def write_temp(path, write):
            if os.path.basename(path) == 'model.pkl':
                def write(f):
                    f.write(b'half a model')
                    raise OSError(errno.ENOSPC, '磁盘空间不足')
            return real_write_temp(path, write)

        monkeypatch.setattr(persist, '_write_temp', write_temp)
        with pytest.raises(ModelSaveError, match=r'^无法保存模型文件到 .+model（磁盘空间不足），原来的模型没有改动$'):
            classifier.train()
        self.assert_kept(data_dir, saved)

    def test_failure_replacing_the_model_puts_the_binarizer_back(self, saved, data_dir, monkeypatch):
        self.fail_replacing_model(monkeypatch, lambda attempt: OSError(errno.EIO, '读写错误'))
        with pytest.raises(ModelSaveError, match=r'（读写错误），原来的模型没有改动$'):
            classifier.train()
        self.assert_kept(data_dir, saved)

    def test_first_save_failing_leaves_no_half_pair(self, movies, data_dir, monkeypatch):
        movies(120, like=True)
        movies(120, like=False)
        self.fail_replacing_model(monkeypatch, lambda attempt: OSError(errno.EIO, '读写错误'))
        with pytest.raises(ModelSaveError):
            classifier.train()
        assert model_folder(data_dir) == []
        with pytest.raises(FileNotFoundError):
            classifier.load()

    def test_file_in_use_is_retried(self, saved, data_dir, monkeypatch):
        attempts = self.fail_replacing_model(
            monkeypatch, lambda attempt: PermissionError(errno.EACCES, '文件正在使用') if attempt <= 2 else None)
        _, scores = classifier.train()
        assert len(attempts) == 3
        model_bytes, binarizer_bytes = self.files(data_dir)
        assert model_bytes != saved[0] and binarizer_bytes != saved[1]
        assert pickle.loads(model_bytes)['scores'] == scores
        assert model_folder(data_dir) == ['label_binarizer.pkl', 'model.pkl']

    def test_file_in_use_for_too_long(self, saved, data_dir, monkeypatch):
        attempts = self.fail_replacing_model(monkeypatch, lambda attempt: PermissionError(errno.EACCES, '文件正在使用'))
        with pytest.raises(ModelSaveError, match=r'（文件正在使用），原来的模型没有改动$'):
            classifier.train()
        assert len(attempts) == len(persist.REPLACE_RETRY_DELAYS) + 1
        self.assert_kept(data_dir, saved)

    def test_model_file_being_read_meanwhile(self, saved, data_dir, monkeypatch):
        '''Windows 上别人正读着 model.pkl 时不能替换它，等一会儿、读完了再替换；别的系统直接替换'''
        reader = open(model_file(data_dir), 'rb')
        waits = []

        def sleep(seconds):
            waits.append(seconds)
            reader.close()

        monkeypatch.setattr(persist, 'time', types.SimpleNamespace(sleep=sleep))
        try:
            classifier.train()
        finally:
            reader.close()
        assert self.files(data_dir)[0] != saved[0]
        assert model_folder(data_dir) == ['label_binarizer.pkl', 'model.pkl']
        if sys.platform == 'win32':
            assert waits == [persist.REPLACE_RETRY_DELAYS[0]]


class TestModelLock:
    def test_recommendation_waits_for_training(self, movies, monkeypatch):
        movies(120, like=True)
        movies(120, like=False)
        movies(10, like=True, rated=False)
        classifier.train()
        saving, release = threading.Event(), threading.Event()
        real_save = classifier.save_model_files

        def slow_save(*args):
            saving.set()
            release.wait(timeout=10)
            real_save(*args)

        monkeypatch.setattr(classifier, 'save_model_files', slow_save)
        results = []
        trainer = threading.Thread(target=classifier.train)
        recommender = threading.Thread(target=lambda: results.append(classifier.recommend()))
        trainer.start()
        try:
            assert saving.wait(timeout=10)
            recommender.start()
            recommender.join(timeout=0.5)
            # 训练还没保存完：推荐在等，没有去读马上要被替换的文件，也没有写系统评分
            assert recommender.is_alive()
            assert system_rated() == 0
        finally:
            release.set()
            trainer.join(timeout=30)
            if recommender.ident is not None:
                recommender.join(timeout=30)
        assert [total for total, _ in results] == [10]
        assert system_rated() == 10


class TestModelFile:
    def save(self, data_dir, content):
        model_file(data_dir).write_bytes(pickle.dumps(content))
        binarizer_file(data_dir).write_bytes(pickle.dumps(BINARIZER))

    @pytest.mark.parametrize('content, detail', [
        (b'not a pickle at all', '模型文件 label_binarizer.pkl 无法读取'),
        (pickle.dumps(KNeighborsClassifier()), '模型文件 label_binarizer.pkl 格式不对'),
        (pickle.dumps(MultiLabelBinarizer()), '模型文件 label_binarizer.pkl 格式不对'),
    ], ids=['unreadable', 'not-a-binarizer', 'never-fitted'])
    def test_binarizer_is_checked_too(self, data_dir, content, detail):
        self.save(data_dir, {'model': MODEL, 'scores': SCORES, 'sklearn_version': sklearn.__version__})
        binarizer_file(data_dir).write_bytes(content)
        with pytest.raises(ModelLoadError, match=detail) as info:
            classifier.load()
        assert '重新训练' in str(info.value)

    def test_missing_binarizer_means_no_model(self, data_dir):
        self.save(data_dir, {'model': MODEL, 'scores': SCORES, 'sklearn_version': sklearn.__version__})
        binarizer_file(data_dir).unlink()
        with pytest.raises(FileNotFoundError):
            classifier.load()

    def test_current_format_loads_quietly(self, data_dir, caplog):
        self.save(data_dir, {'model': MODEL, 'scores': SCORES, 'sklearn_version': sklearn.__version__})
        model, scores = classifier.load()
        assert isinstance(model, KNeighborsClassifier)
        assert scores == SCORES
        assert warning_messages(caplog) == []

    def test_model_that_was_never_trained(self, data_dir):
        self.save(data_dir, {'model': KNeighborsClassifier(), 'scores': SCORES,
                             'sklearn_version': sklearn.__version__})
        with pytest.raises(ModelLoadError) as info:
            classifier.load()
        assert str(info.value) == PREDICT_ERROR

    def test_old_tuple_format_loads_with_a_hint(self, data_dir, caplog):
        self.save(data_dir, (MODEL, SCORES))
        assert classifier.load()[1] == SCORES
        assert any('旧版本程序保存' in m and '建议' in m for m in warning_messages(caplog))

    def test_other_sklearn_version_loads_with_a_hint(self, data_dir, caplog):
        self.save(data_dir, {'model': MODEL, 'scores': SCORES, 'sklearn_version': '0.21.3'})
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
