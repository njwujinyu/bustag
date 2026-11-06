'''
模型文件的保存和读取。读取时检查内容，网页、推荐和 check 对同一个文件的判断一致
'''
import numbers
import os
import pickle

from bustag.util import MODEL_PATH

MODEL_FILE = MODEL_PATH + 'model.pkl'
BINARIZER_PATH = MODEL_PATH + 'label_binarizer.pkl'
SCORE_KEYS = ('precision', 'recall', 'f1')
RETRAIN_HINT = '请到网页的"模型"页点击"开始训练"重新训练'


class ModelLoadError(Exception):
    '''模型文件存在但读不了或者内容不对（多半是升级了 scikit-learn，或者文件损坏），需要重新训练'''


def dump_model(path, models):
    '''
    Args:
        models: tuple of models to save
    '''
    with open(path, 'wb') as f:
        pickle.dump(models, f)


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
