'''
模型文件的保存和读取。模型存成一个 JSON 文件（每个标签的权重、截距和训练时的检验结果）；
网页"模型"页、打分和 check 都用 read_model 判断模型能不能用
'''
import json
import numbers
import os
import threading
from dataclasses import asdict, dataclass

from bustag.util import MODEL_PATH, os_error_text, save_file

MODEL_FILE = MODEL_PATH + 'model.json'
FORMAT_VERSION = 1
# 旧版本程序把模型存成这两个 pickle 文件。新版本不读它们（内容和现在的模型不是一回事），只凭文件在不在判断
LEGACY_NAMES = ('model.pkl', 'label_binarizer.pkl')
# 模型文件一般几十 KB，标签上万个也只有 1 MB 左右；比这大得多的肯定不是程序保存的，不读进来
MAX_FILE_SIZE = 64 * 1024 * 1024
# 权重（每个标签的 idf 和系数）、截距实际只有个位数到两位数，打标条数、折数、次数这些整数也不会有多大：
# 绝对值超过这两个上限的肯定不是程序保存的，按格式不对处理
MAX_WEIGHT = 1e6
MAX_COUNT = 10 ** 12
RETRAIN_HINT = '请到网页的"模型"页点击"开始训练"重新训练'
# 读模型文件和"把它换成新的"那一下不能同时进行：Windows 上正在被替换的文件打不开（读的一方会把刚训练好的
# 模型当成读不了），正开着的文件也换不掉。读的那几毫秒、换的那一下各拿一次这把锁；保存的一方等着重试的时候不拿，
# 所以读的一方最多等一次替换，读到的总是完整的旧文件或者新文件
_swap = threading.Lock()
LEGACY_MODEL = f'现有的模型文件是旧版本程序训练的，新版本换了模型，不能再用。{RETRAIN_HINT}'


@dataclass(frozen=True)
class Report:
    '''
    训练时的检验结果。likes、dislikes 是训练用的"喜欢""不喜欢"打标条数；交叉验证把打标分成 folds 份、重复 repeats 次；
    like_recall、dislike_recall 是用户喜欢、不喜欢的影片里各认对了多少（0 到 1）；
    auc 是按分数排序时，喜欢的影片排在不喜欢的前面的概率（0 到 1，乱排是 0.5）
    '''
    likes: int
    dislikes: int
    folds: int
    repeats: int
    like_recall: float
    dislike_recall: float
    auc: float


@dataclass(frozen=True)
class SavedModel:
    '''
    保存下来的模型。tags 是"标签值 → (idf, 系数)"，intercept 是截距，打分的算法见 classifier.score_tags；
    report 是训练时的检验结果，trained_at 是训练的时间
    '''
    intercept: float
    tags: dict
    report: Report
    trained_at: str


class ModelLoadError(Exception):
    '''模型文件存在但不能用（读不了、内容不对，或者是旧版本程序训练的），需要重新训练；消息是中文，可以直接显示给用户'''


class LegacyModelError(ModelLoadError):
    '''数据目录里只有旧版本程序训练的模型文件'''


class ModelSaveError(Exception):
    '''训练好的模型没能保存（磁盘满、没有写权限、文件一直被占用等）；消息是中文，可以直接显示给用户'''


def save_model(path, model):
    '''
    把 model（SavedModel）存成 path：先写同目录的临时文件再替换，别处读到的要么是原来的文件、要么是新的；
    失败时原来的文件不变，也不留下临时文件

    Raises:
        ModelSaveError: 写不了文件，消息是中文，可以直接显示给用户
    '''
    model_dir = os.path.normpath(os.path.dirname(path))
    content = {'version': FORMAT_VERSION, 'trained_at': model.trained_at, 'report': asdict(model.report),
               'intercept': model.intercept, 'tags': {tag: list(weights) for tag, weights in model.tags.items()}}
    data = json.dumps(content, ensure_ascii=False, allow_nan=False).encode('utf-8')
    try:
        os.makedirs(model_dir, exist_ok=True)
        save_file(path, data, swap=_swap)
    except OSError as ex:
        raise ModelSaveError(f'无法保存模型文件到 {model_dir}（{os_error_text(ex)}），原来的模型没有改动') from ex


def _unreadable(path, reason):
    return ModelLoadError(f'模型文件 {os.path.basename(path)} 无法读取（{reason}）。{RETRAIN_HINT}')


def _number(value, minimum=-MAX_WEIGHT, maximum=MAX_WEIGHT):
    '''
    value 是数字（不算 True / False）、不是 NaN、在 minimum 和 maximum 之间时，返回它的小数形式，否则返回 None。
    整数也换成小数：JSON 里可以写任意大的整数，留着整数去算分数，到开方、相除的时候会溢出
    '''
    if not isinstance(value, numbers.Real) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if minimum <= number <= maximum else None


def _is_count(value, minimum=0):
    return type(value) is int and minimum <= value <= MAX_COUNT


def _report_from(data):
    '''文件里的检验结果换成 Report，内容不对时返回 None'''
    if not isinstance(data, dict):
        return None
    # 两类打标都有才训练得出模型；交叉验证至少分 2 份、做 1 次
    counts = [(data.get('likes'), 1), (data.get('dislikes'), 1), (data.get('folds'), 2), (data.get('repeats'), 1)]
    shares = [_number(data.get(key), 0, 1) for key in ('like_recall', 'dislike_recall', 'auc')]
    if not all(_is_count(value, minimum) for value, minimum in counts) or None in shares:
        return None
    return Report(*(value for value, _ in counts), *shares)


def _model_from(data):
    '''文件的内容（已经解析成 JSON）换成 SavedModel，内容不对时返回 None'''
    if not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != FORMAT_VERSION:
        return None
    intercept, tags, trained_at = _number(data.get('intercept')), data.get('tags'), data.get('trained_at')
    report = _report_from(data.get('report'))
    if report is None or intercept is None or not isinstance(trained_at, str):
        return None
    if not isinstance(tags, dict) or not tags:
        return None
    weights = {}
    for tag, pair in tags.items():
        if not isinstance(pair, list) or len(pair) != 2:
            return None
        idf, coefficient = _number(pair[0]), _number(pair[1])
        # idf 是正数：它既是标签的权重，也用来算一部影片的标签向量有多长
        if idf is None or idf <= 0 or coefficient is None:
            return None
        weights[tag] = (idf, coefficient)
    return SavedModel(intercept, weights, report, trained_at)


def _has_legacy_files(model_dir):
    return any(os.path.exists(os.path.join(model_dir, name)) for name in LEGACY_NAMES)


def read_model(path):
    '''
    读取并检查模型文件 path，返回 SavedModel

    Raises:
        FileNotFoundError: 还没有训练过模型
        LegacyModelError: 没有新的模型文件，只有旧版本程序训练的（旧文件不读）
        ModelLoadError: 文件读不了或者内容不对；消息是中文，可以直接显示给用户
    '''
    try:
        with _swap, open(path, 'rb') as f:
            raw = f.read(MAX_FILE_SIZE + 1)
    except FileNotFoundError:
        if _has_legacy_files(os.path.dirname(path)):
            raise LegacyModelError(LEGACY_MODEL) from None
        raise
    except OSError as ex:
        raise _unreadable(path, os_error_text(ex)) from ex
    model = None
    if len(raw) <= MAX_FILE_SIZE:
        try:
            model = _model_from(json.loads(raw.decode('utf-8')))
        except (ValueError, RecursionError) as ex:
            raise _unreadable(path, '不是有效的 JSON，文件可能已经损坏') from ex
    if model is None:
        raise ModelLoadError(f'模型文件 {os.path.basename(path)} 格式不对，{RETRAIN_HINT}')
    return model
