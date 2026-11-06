'''
prepare data for model training
'''
import warnings
from collections import Counter

from sklearn.model_selection import train_test_split
from sklearn.preprocessing import MultiLabelBinarizer
from bustag.spider.db import get_items, RATE_TYPE
from bustag.model.persist import prediction_error
from bustag.util import logger


def load_data():
    '''
    load data from database and do processing
    '''
    rate_type = RATE_TYPE.USER_RATE.value
    rate_value = None
    page = None
    items, _ = get_items(rate_type=rate_type, rate_value=rate_value,
                         page=page)
    return items


def as_dict(item):
    tags_set = set()
    for tags in item.tags_dict.values():
        for tag in tags:
            tags_set.add(tag)
    d = {
        'id': item.fanhao,
        'title': item.title,
        'fanhao': item.fanhao,
        'url': item.url,
        'add_date': item.add_date,
        'tags': tags_set,
        'cover_img_url': item.cover_img_url,
        'target': item.rate_value
    }
    return d


def check_labels(y):
    '''
    "喜欢"和"不喜欢"都要有（每种至少 2 条），分层抽样才能让训练集和测试集里都有两种标签

    Raises:
        ValueError: 消息是中文，可以直接显示给用户
    '''
    counts = Counter(y)
    if not counts:
        raise ValueError('还没有打标数据, 无法训练模型')
    if len(counts) == 1:
        label = '喜欢' if 1 in counts else '不喜欢'
        raise ValueError(f'打标数据里只有"{label}"一种标签, 无法训练模型. 请把"喜欢"和"不喜欢"都打一些')
    if min(counts.values()) < 2:
        raise ValueError('"喜欢"和"不喜欢"都至少需要 2 条打标数据, 才能训练模型')


def process_data(rows):
    '''
    do all processing , like onehotencode tag string

    Args:
        rows: as_dict() 返回的字典列表

    Returns:
        (X, y, 标签编码器)。编码器在模型训练成功后和模型一起保存
    '''
    mlb = MultiLabelBinarizer()
    X = mlb.fit_transform([row['tags'] for row in rows])
    y = [row['target'] for row in rows]
    return X, y, mlb


def split_data(X, y):
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.25, random_state=42, stratify=y)
    return (X_train, X_test, y_train, y_test)


def prepare_data(min_samples=0):
    '''
    Args:
        min_samples: 打标数据少于这个数量时不训练

    Returns:
        (X_train, X_test, y_train, y_test, 标签编码器)

    Raises:
        ValueError: 打标数据不够，或者只有一种标签。消息是中文，可以直接显示给用户
    '''
    items = load_data()
    total = len(items)
    if total < min_samples:
        raise ValueError(f'训练数据不足, 无法训练模型. 需要{min_samples}, 当前{total}')
    rows = [as_dict(item) for item in items]
    check_labels([row['target'] for row in rows])
    X, y, mlb = process_data(rows)
    return (*split_data(X, y), mlb)


def prepare_predict_data(mlb):
    '''
    还没有评分的影片，返回 (番号列表, 编码后的标签)

    Args:
        mlb: 和模型一起保存的标签编码器

    Raises:
        ModelLoadError: 编码器用不了（和当前的 scikit-learn 不兼容等），需要重新训练
    '''
    # get not rated data
    rate_type = None
    rate_value = None
    page = None
    unrated_items, _ = get_items(
        rate_type=rate_type, rate_value=rate_value, page=page)
    rows = [as_dict(item) for item in unrated_items]
    tag_sets = [row['tags'] for row in rows]
    try:
        unknown = set().union(*tag_sets) - set(mlb.classes_)
        with warnings.catch_warnings():
            # 新影片总有训练时没见过的演员、系列，忽略它们正是想要的结果；
            # scikit-learn 每次都会把这些标签名整串打成警告，看起来像出错
            warnings.filterwarnings('ignore', message='unknown class', category=UserWarning)
            X = mlb.transform(tag_sets)
    except Exception as ex:
        raise prediction_error(ex) from ex
    if unknown:
        logger.debug(f'本次有 {len(unknown)} 个训练时没见过的标签，已忽略')
    return [row['id'] for row in rows], X
