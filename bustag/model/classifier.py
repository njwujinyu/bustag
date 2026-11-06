'''
create classifier model and predict
'''
import threading

import sklearn
from peewee import chunked
from sklearn.metrics import f1_score, recall_score, precision_score, confusion_matrix
from sklearn.neighbors import KNeighborsClassifier
from bustag.model.prepare import prepare_data, prepare_predict_data
from bustag.model.persist import BINARIZER_PATH, MODEL_FILE, prediction_error, read_model_files, save_model_files
from bustag.spider.db import RATE_TYPE, ItemRate, db
from bustag.util import logger, get_data_path

MIN_TRAIN_NUM = 200
# 推荐结果分批插入，每批的行数受 SQLite 单条语句参数个数的限制
INSERT_BATCH = 100
# 训练和推荐互斥：推荐从读模型文件到写完系统评分都用同一对文件，训练不能在这期间替换它们；
# 两次训练（双击按钮、开了两个标签页）也不会交错写文件
_model_lock = threading.Lock()


def _read_saved():
    '''读取保存的模型和标签编码器（见 read_model_files），训练时的 scikit-learn 版本不同时写警告日志'''
    saved = read_model_files(get_data_path(MODEL_FILE), get_data_path(BINARIZER_PATH))
    if saved.sklearn_version is None:
        logger.warning(f'模型文件是旧版本程序保存的，没有记录 scikit-learn 版本（当前是 {sklearn.__version__}），'
                       '建议到网页的"模型"页重新训练')
    elif saved.sklearn_version != sklearn.__version__:
        logger.warning(f'模型是用 scikit-learn {saved.sklearn_version} 训练的，当前是 {sklearn.__version__}，'
                       '建议到网页的"模型"页重新训练')
    return saved


def load():
    '''
    读取保存的模型（模型和标签编码器两个文件都要能用，并且能一起预测），返回 (model, scores)

    Raises:
        FileNotFoundError: 还没有训练过模型
        ModelLoadError: 模型文件或标签编码器读不了、格式不对、对不上或者不能预测，需要重新训练
    '''
    saved = _read_saved()
    return saved.model, saved.scores


def create_model():
    knn = KNeighborsClassifier(n_neighbors=11)
    return knn


def train():
    '''
    用用户的打标数据训练，评估完才保存（替换原来的模型文件），返回 (model, scores)

    Raises:
        ValueError: 打标数据不够或者只有一种标签，消息是中文，可以直接显示给用户
        ModelSaveError: 模型没能保存，原来的模型文件没有改动，消息是中文
    '''
    with _model_lock:
        model = create_model()
        X_train, X_test, y_train, y_test, binarizer = prepare_data(MIN_TRAIN_NUM)
        model.fit(X_train, y_train)
        y_pred = model.predict(X_test)
        confusion_mtx = confusion_matrix(y_test, y_pred, labels=[0, 1])
        scores = evaluate(confusion_mtx, y_test, y_pred)
        save_model_files(get_data_path(MODEL_FILE),
                         {'model': model, 'scores': scores, 'sklearn_version': sklearn.__version__},
                         get_data_path(BINARIZER_PATH), binarizer)
    logger.info('模型训练完成')
    return model, scores


def evaluate(confusion_mtx, y_test, y_pred):
    tn, fp, fn, tp = confusion_mtx.ravel()
    precision = precision_score(y_test, y_pred, zero_division=0)
    recall = recall_score(y_test, y_pred, zero_division=0)
    f1 = f1_score(y_test, y_pred, zero_division=0)
    logger.info(f'tp: {tp}, fp: {fp}')
    logger.info(f'fn: {fn}, tn: {tn}')
    logger.info(f'precision_score: {precision}')
    logger.info(f'recall_score: {recall}')
    logger.info(f'f1_score: {f1}')
    model_scores = dict(precision=precision, recall=recall, f1=f1)
    model_scores = {key: float('{:.2f}'.format(value))
                    for key, value in model_scores.items()}
    return model_scores


def recommend():
    '''
    用训练好的模型给还没打标的影片打系统评分，返回 (预测部数, 推荐部数)；没有新影片时返回 (0, 0)。
    取数据之后、写入之前用户打了标的影片保留用户的打标

    Raises:
        FileNotFoundError: 还没有训练过模型
        ModelLoadError: 模型或标签编码器读不了、格式不对、对不上，或者预测出错，需要重新训练；这时不写系统评分
    '''
    with _model_lock:
        saved = _read_saved()
        ids, X = prepare_predict_data(saved.binarizer)
        if not ids:
            logger.info('没有需要推荐的新影片')
            return 0, 0
        total = len(ids)
        try:
            y_pred = [int(y) for y in saved.model.predict(X)]
        except Exception as ex:
            raise prediction_error(ex) from ex
        count = sum(y_pred)
        rows = [{'item': fanhao, 'rate_type': RATE_TYPE.SYSTEM_RATE, 'rate_value': y}
                for fanhao, y in zip(ids, y_pred)]
        written = 0
        with db.atomic():
            for batch in chunked(rows, INSERT_BATCH):
                written += ItemRate.insert_many(batch).on_conflict_ignore().as_rowcount().execute()
    logger.info(f'推荐完成：预测 {total} 部，推荐 {count} 部')
    if written < total:
        logger.info(f'跳过 {total - written} 部（推荐期间已被打标）')
    return total, count
