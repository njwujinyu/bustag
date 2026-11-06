'''
persist model required files
'''
import os
import pickle


class ModelLoadError(Exception):
    '''模型文件存在但读不了（多半是升级了 scikit-learn，或者文件损坏），需要重新训练'''


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
            '多半是 scikit-learn 版本升级造成的。请到网页的"模型"页点击"开始训练"重新训练') from ex
