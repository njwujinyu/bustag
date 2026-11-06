'''
模型文件的保存和读取。读取时检查内容并试预测一次；网页"模型"页、推荐和 check 都用 read_model_files 判断模型能不能用
'''
import numbers
import os
import pickle
import tempfile
import time
from collections import namedtuple

from bustag.util import MODEL_PATH, logger

MODEL_FILE = MODEL_PATH + 'model.pkl'
BINARIZER_PATH = MODEL_PATH + 'label_binarizer.pkl'
SCORE_KEYS = ('precision', 'recall', 'f1')
RETRAIN_HINT = '请到网页的"模型"页点击"开始训练"重新训练'
PREDICT_ERROR = f'模型和标签编码器对不上，或者和当前的 scikit-learn 不兼容，无法预测。{RETRAIN_HINT}'
# Windows 上目标文件正被读取（或者被杀毒软件扫描）时 os.replace 会 PermissionError，按这些间隔（秒）重试
REPLACE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)

SavedModel = namedtuple('SavedModel', 'model scores sklearn_version binarizer')


class ModelLoadError(Exception):
    '''模型文件存在但读不了、内容不对或者不能用来预测（多半是升级了 scikit-learn，或者文件损坏），需要重新训练'''


class ModelSaveError(Exception):
    '''训练好的模型没能保存（磁盘满、没有写权限、文件一直被占用等）；消息是中文，可以直接显示给用户'''


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _write_temp(path, write):
    '''在 path 所在目录建临时文件，用 write(文件对象) 写入并落盘，返回临时文件路径；写入失败时删掉临时文件'''
    fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(path), prefix=os.path.basename(path) + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as f:
            write(f)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        _remove(temp_path)
        raise
    return temp_path


def _replace(source, target):
    '''用 source 原子地替换 target；target 暂时被占用（PermissionError）时等一会儿重试'''
    for delay in REPLACE_RETRY_DELAYS:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            time.sleep(delay)
    os.replace(source, target)


def _read_bytes(path):
    try:
        with open(path, 'rb') as f:
            return f.read()
    except FileNotFoundError:
        return None


def _put_back(path, content):
    '''把 path 恢复成 content（None 表示原来没有这个文件），成功返回 True'''
    try:
        if content is None:
            os.remove(path)
            return True
        temp_path = _write_temp(path, lambda f: f.write(content))
        try:
            _replace(temp_path, path)
        finally:
            _remove(temp_path)
    except FileNotFoundError:
        return content is None
    except OSError:
        return False
    return True


def _save_error(model_dir, ex, outcome):
    return ModelSaveError(f'无法保存模型文件到 {model_dir}（{ex.strerror or ex}），{outcome}')


def save_model_files(model_path, model_data, binarizer_path, binarizer):
    '''
    保存训练结果：model_data（模型、评分、scikit-learn 版本）写进 model.pkl，标签编码器写进 label_binarizer.pkl。
    两个文件必须是同一次训练的：先把两个都写进同目录的临时文件，再逐个替换；任何一步失败都保留原来的一对
    （已经替换的标签编码器会换回去），不会留下写了一半的文件或者新旧不配对的一对

    Raises:
        ModelSaveError: 写不了文件，消息是中文，可以直接显示给用户
    '''
    model_dir = os.path.normpath(os.path.dirname(model_path))
    temps = []
    try:
        os.makedirs(model_dir, exist_ok=True)
        temps.append(_write_temp(binarizer_path, lambda f: pickle.dump(binarizer, f)))
        temps.append(_write_temp(model_path, lambda f: pickle.dump(model_data, f)))
        previous_binarizer = _read_bytes(binarizer_path)
        _replace(temps[0], binarizer_path)
        try:
            _replace(temps[1], model_path)
        except OSError as ex:
            if not _put_back(binarizer_path, previous_binarizer):
                raise _save_error(model_dir, ex, '标签编码器也没能恢复，请重新训练') from ex
            raise
    except OSError as ex:
        raise _save_error(model_dir, ex, '原来的模型没有改动') from ex
    finally:
        for temp_path in temps:
            _remove(temp_path)


def load_model(path):
    with open(path, 'rb') as f:
        models = pickle.load(f)
    return models


def read_model(path):
    '''
    读取模型文件。文件不存在时抛 FileNotFoundError；
    文件存在但读不了时抛 ModelLoadError，消息是中文，可以直接显示给用户
    '''
    try:
        return load_model(path)
    except FileNotFoundError:
        raise
    except Exception as ex:
        raise ModelLoadError(
            f'模型文件 {os.path.basename(path)} 无法读取（{type(ex).__name__}: {ex}），'
            f'多半是 scikit-learn 版本升级造成的。{RETRAIN_HINT}') from ex


def _format_error(path):
    return ModelLoadError(f'模型文件 {os.path.basename(path)} 格式不对, {RETRAIN_HINT}')


def _is_number(value):
    return isinstance(value, numbers.Real) and not isinstance(value, bool)


def read_classifier(path):
    '''
    读取 model.pkl，返回 (model, scores, 训练时的 scikit-learn 版本)。
    旧版本程序保存的是 (model, scores) 二元组，没有记录版本，这时版本返回 None

    Raises:
        FileNotFoundError: 还没有训练过模型
        ModelLoadError: 读不了，或者内容不是能预测的模型加 precision / recall / f1 评分
    '''
    data = read_model(path)
    if isinstance(data, dict):
        model, scores, version = data.get('model'), data.get('scores'), data.get('sklearn_version')
    elif isinstance(data, (tuple, list)) and len(data) == 2:
        (model, scores), version = data, None
    else:
        raise _format_error(path)
    if not callable(getattr(model, 'predict', None)):
        raise _format_error(path)
    if not isinstance(scores, dict) or not all(_is_number(scores.get(key)) for key in SCORE_KEYS):
        raise _format_error(path)
    return model, scores, version


def read_binarizer(path):
    '''
    读取 label_binarizer.pkl（训练时生成的标签编码器）

    Raises:
        FileNotFoundError: 还没有训练过模型
        ModelLoadError: 读不了，或者内容不是标签编码器
    '''
    binarizer = read_model(path)
    if not callable(getattr(binarizer, 'transform', None)) or not hasattr(binarizer, 'classes_'):
        raise _format_error(path)
    return binarizer


def prediction_error(ex):
    '''编码标签或者预测出错时抛出的错误：界面上统一显示 PREDICT_ERROR，原来的异常只写进 DEBUG 日志'''
    logger.debug(f'预测出错：{type(ex).__name__}: {ex}')
    return ModelLoadError(PREDICT_ERROR)


def check_prediction(model, binarizer):
    '''
    确认模型和标签编码器是同一次训练保存的、在当前的 scikit-learn 下能用：模型的特征数要等于编码器的标签数，
    再拿一部没有标签的影片试预测一次

    Raises:
        ModelLoadError: 对不上或者预测出错，消息是 PREDICT_ERROR
    '''
    try:
        n_features = getattr(model, 'n_features_in_', None)
        if n_features is not None and n_features != len(binarizer.classes_):
            raise ValueError(f'模型需要 {n_features} 个特征，标签编码器有 {len(binarizer.classes_)} 个标签')
        model.predict(binarizer.transform([[]]))
    except Exception as ex:
        raise prediction_error(ex) from ex


def read_model_files(model_path, binarizer_path):
    '''
    读取并检查训练生成的两个文件（含配对检查和试预测），推荐时两个都要用到。
    返回 SavedModel：model.pkl 里的模型、评分、训练时的 scikit-learn 版本，以及标签编码器

    Raises:
        FileNotFoundError: 还没有训练过模型（缺任何一个文件）
        ModelLoadError: 任何一个文件读不了、内容不对，或者两个对不上、不能预测；消息是中文，可以直接显示给用户
    '''
    model, scores, version = read_classifier(model_path)
    binarizer = read_binarizer(binarizer_path)
    check_prediction(model, binarizer)
    return SavedModel(model, scores, version, binarizer)
