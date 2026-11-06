'''
create classifier model and predict
'''
import sklearn
from sklearn.metrics import f1_score, recall_score, accuracy_score, precision_score, confusion_matrix
from sklearn.neighbors import KNeighborsClassifier
from bustag.model.prepare import prepare_data, prepare_predict_data
from bustag.model.persist import MODEL_FILE, dump_model, read_classifier
from bustag.spider.db import RATE_TYPE, ItemRate
from bustag.util import logger, get_data_path

MIN_TRAIN_NUM = 200


def load():
    '''
    读取保存的模型，返回 (model, scores)

    Raises:
        FileNotFoundError: 还没有训练过模型
        ModelLoadError: 模型文件读不了或者格式不对，需要重新训练
    '''
    model, scores, saved_version = read_classifier(get_data_path(MODEL_FILE))
    if saved_version is None:
        logger.warning(f'模型文件是旧版本程序保存的，没有记录 scikit-learn 版本（当前是 {sklearn.__version__}），'
                       '建议到网页的"模型"页重新训练')
    elif saved_version != sklearn.__version__:
        logger.warning(f'模型是用 scikit-learn {saved_version} 训练的，当前是 {sklearn.__version__}，'
                       '建议到网页的"模型"页重新训练')
    return model, scores


def create_model():
    knn = KNeighborsClassifier(n_neighbors=11)
    return knn


def train():
    '''
    Raises:
        ValueError: 打标数据不够或者只有一种标签，消息是中文，可以直接显示给用户
    '''
    model = create_model()
    X_train, X_test, y_train, y_test = prepare_data(MIN_TRAIN_NUM)
    model.fit(X_train, y_train)
    y_pred = model.predict(X_test)
    confusion_mtx = confusion_matrix(y_test, y_pred, labels=[0, 1])
    scores = evaluate(confusion_mtx, y_test, y_pred)
    dump_model(get_data_path(MODEL_FILE),
               {'model': model, 'scores': scores, 'sklearn_version': sklearn.__version__})
    logger.info('模型训练完成')
    return model, scores


def evaluate(confusion_mtx, y_test, y_pred):
    tn, fp, fn, tp = confusion_mtx.ravel()
    # accuracy = accuracy_score(y_test, y_pred)
    precision = precision_score(y_test, y_pred, zero_division=0)
    recall = recall_score(y_test, y_pred, zero_division=0)
    f1 = f1_score(y_test, y_pred, zero_division=0)
    logger.info(f'tp: {tp}, fp: {fp}')
    logger.info(f'fn: {fn}, tn: {tn}')
    # logger.info(f'accuracy_score: {accuracy}')
    logger.info(f'precision_score: {precision}')
    logger.info(f'recall_score: {recall}')
    logger.info(f'f1_score: {f1}')
    model_scores = dict(precision=precision, recall=recall, f1=f1)
    model_scores = {key: float('{:.2f}'.format(value))
                    for key, value in model_scores.items()}
    return model_scores


def recommend():
    '''
    用训练好的模型给还没打标的影片打系统评分，返回 (预测部数, 推荐部数)；没有新影片时返回 (0, 0)

    Raises:
        FileNotFoundError: 还没有训练过模型
        ModelLoadError: 模型或标签编码器读不了、格式不对，需要重新训练
    '''
    model, _ = load()
    ids, X = prepare_predict_data()
    if not ids:
        logger.info('没有需要推荐的新影片')
        return 0, 0
    total = len(ids)
    y_pred = model.predict(X)
    count = 0
    for id, y in zip(ids, y_pred):
        if y == 1:
            count += 1
        rate_type = RATE_TYPE.SYSTEM_RATE
        rate_value = y
        item_id = id
        item_rate = ItemRate(rate_type=rate_type,
                             rate_value=rate_value, item_id=item_id)
        item_rate.save()
    logger.info(f'推荐完成：预测 {total} 部，推荐 {count} 部')
    return total, count
