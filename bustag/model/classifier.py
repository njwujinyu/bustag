'''
模型：用用户的打标训练、检验并保存；用保存的模型给还不是用户打标的影片打分，按分数划分"推荐"和"不太像"。
模型是带类别权重的逻辑回归，输入是影片的标签（0/1 编码后做 TF-IDF）。
scikit-learn 只在训练时用到、用到时才导入（它导入较慢）；打分按公式直接算
'''
import math
import threading

from peewee import OperationalError

from bustag.model import prepare
from bustag.model.persist import MODEL_FILE, Report, SavedModel, read_model, save_model
from bustag.spider import db
from bustag.util import format_datetime, get_data_path, get_now_time, logger

# 交叉验证最多分成这么多份（少的那一类没有这么多条时按它的条数分），重复这么多次；
# 种子固定，同样的打标每次得到同样的检验结果
CV_FOLDS = 5
CV_REPEATS = 10
CV_SEED = 0
# 逻辑回归的正则强度（越大约束越松）
REGULARIZATION = 10.0
# 打了分的影片里，分数最高的这个百分比算"推荐"；不足 MIN_RECOMMENDED 部时取 MIN_RECOMMENDED 部
RECOMMENDED_PERCENT = 30
MIN_RECOMMENDED = 20
# 每部影片最多记这么多个拉高（拉低）分数的标签；对分数的影响（对数几率）不到 MIN_REASON_WEIGHT 的不记
MAX_REASONS = 2
MIN_REASON_WEIGHT = 0.05
# 模型和"所有影片都猜成打标多的那一类"没有差别的两种情况：交叉验证里少的那一类认对不到 GUESSING_RECALL；
# 或者少的那一类认对不到 SOUND_RECALL，同时打过分的影片里超过 GUESSING_SHARE 都判到了多的那一边
# （打过分的不到 MIN_SCORED_TO_JUDGE 部时不看这一条）。少的那一类认对了 SOUND_RECALL 以上，说明模型分得开：
# 这时影片都在一边是因为它们本来就像（例如都是喜欢的演员的作品），不算模型没有筛选
GUESSING_RECALL = 0.1
SOUND_RECALL = 0.5
GUESSING_SHARE = 0.98
MIN_SCORED_TO_JUDGE = 50
# 训练和打分互斥：一次打分从读模型文件到写完分数用的是同一个模型，训练不能在这期间替换它；
# 两次训练（双击按钮、开了两个标签页）、几处触发的打分也不会交错
_model_lock = threading.Lock()


def load():
    '''
    读取保存的模型，返回 SavedModel

    Raises:
        FileNotFoundError: 还没有训练过模型
        ModelLoadError: 模型文件读不了、内容不对，或者是旧版本程序训练的（LegacyModelError），需要重新训练
    '''
    return read_model(get_data_path(MODEL_FILE))


def _encode(rows):
    '''打标数据 [(标签值的集合, 1 / 0)] → (X, y, 标签值的列表)。X 是稀疏的 0/1 矩阵，一行一部影片、一列一个标签值'''
    import numpy
    from sklearn.preprocessing import MultiLabelBinarizer

    binarizer = MultiLabelBinarizer(sparse_output=True)
    X = binarizer.fit_transform([tags for tags, _ in rows]).tocsr()
    y = numpy.array([target for _, target in rows])
    return X, y, [str(name) for name in binarizer.classes_]


def _fit(X, y):
    '''TF-IDF 加逻辑回归。两类的打标条数常常相差很多，按类别加权，少的那一类不会被淹没'''
    from sklearn.feature_extraction.text import TfidfTransformer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline

    regression = LogisticRegression(class_weight='balanced', C=REGULARIZATION, max_iter=5000)
    return make_pipeline(TfidfTransformer(), regression).fit(X, y)


def _cross_validate(X, y, folds):
    '''
    重复分层交叉验证，返回 (喜欢认对的比例, 不喜欢认对的比例, 排序 AUC)。
    每一折只用训练部分出现过的标签，和打分时"模型没见过的标签不算"一致
    '''
    import numpy
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import RepeatedStratifiedKFold

    splitter = RepeatedStratifiedKFold(n_splits=folds, n_repeats=CV_REPEATS, random_state=CV_SEED)
    scores = numpy.zeros(len(y))
    likes_right = dislikes_right = 0
    aucs = []
    for number, (train, test) in enumerate(splitter.split(X, y), start=1):
        seen = numpy.asarray(X[train].sum(axis=0)).ravel() > 0
        fitted = _fit(X[train][:, seen], y[train])
        scores[test] = fitted.predict_proba(X[test][:, seen])[:, 1]
        if number % folds == 0:
            # 一次重复做完，每条打标都被没见过它的模型打过一次分
            likes_right += int((scores[y == 1] >= 0.5).sum())
            dislikes_right += int((scores[y == 0] < 0.5).sum())
            aucs.append(float(roc_auc_score(y, scores)))
    likes = int((y == 1).sum())
    return (likes_right / (likes * CV_REPEATS), dislikes_right / ((len(y) - likes) * CV_REPEATS),
            sum(aucs) / len(aucs))


def _train():
    rows = prepare.load_labeled()
    likes = sum(target for _, target in rows)
    dislikes = len(rows) - likes
    problem = prepare.training_problem(likes, dislikes)
    if problem:
        raise ValueError(problem)
    X, y, names = _encode(rows)
    if not names:
        raise ValueError('打标的影片都没有标签，无法训练模型')
    folds = min(CV_FOLDS, likes, dislikes)
    like_recall, dislike_recall, auc = _cross_validate(X, y, folds)
    # 检验只是为了知道模型有多准；真正保存的模型用全部打标拟合
    fitted = _fit(X, y)
    tfidf, regression = fitted[0], fitted[1]
    tags = {name: (float(idf), float(coefficient))
            for name, idf, coefficient in zip(names, tfidf.idf_, regression.coef_[0])}
    report = Report(likes, dislikes, folds, CV_REPEATS, like_recall, dislike_recall, auc)
    saved = SavedModel(float(regression.intercept_[0]), tags, report, format_datetime(get_now_time()))
    logger.info(f'用 {likes + dislikes} 条打标训练（喜欢 {likes}、不喜欢 {dislikes}）。'
                f'交叉验证（{folds} 折 × {CV_REPEATS} 次）：喜欢认对 {like_recall:.0%}，'
                f'不喜欢认对 {dislike_recall:.0%}，排序 AUC {auc:.2f}')
    save_model(get_data_path(MODEL_FILE), saved)
    if prepare.for_reference_only(likes, dislikes):
        name, count = prepare.smaller_class(likes, dislikes)
        logger.warning(f'"{name}"的打标只有 {count} 条（不到 {prepare.SOLID_MINORITY} 条），这个模型的结果仅供参考')
    logger.info('模型训练完成')
    return saved


def train():
    '''
    用全部用户打标训练：先交叉验证得到检验结果，再用全部打标拟合；保存成功才替换原来的模型文件。返回 SavedModel

    Raises:
        ValueError: 打标数据还不够训练，消息是中文，可以直接显示给用户
        ModelSaveError: 模型没能保存，原来的模型文件没有改动，消息是中文
    '''
    with _model_lock:
        return _train()


def _sigmoid(value):
    if value >= 0:
        return 1 / (1 + math.exp(-value))
    # 负得很多时 exp(-value) 会溢出，换一个等价的写法
    exponent = math.exp(value)
    return exponent / (1 + exponent)


def score_tags(model, tags):
    '''
    给带这些标签值的影片打分，返回 (分数, 拉高分数的标签, 拉低分数的标签)。分数在 0 到 1 之间，越高越像用户喜欢的。
    只算模型认识的标签：每个标签的贡献是 idf × 系数 ÷ 这部影片的 idf 向量的长度，分数是 sigmoid(截距 + 各标签的贡献之和)，
    和训练用的 TF-IDF 加逻辑回归算出来的一样。两个列表都按影响从大到小排，各最多 MAX_REASONS 个
    '''
    known = [(tag, *model.tags[tag]) for tag in tags if tag in model.tags]
    length = math.sqrt(sum(idf * idf for _, idf, _ in known))
    parts = sorted(((idf * coefficient / length, tag) for tag, idf, coefficient in known), reverse=True)
    score = _sigmoid(model.intercept + sum(weight for weight, _ in parts))
    because = [tag for weight, tag in parts[:MAX_REASONS] if weight >= MIN_REASON_WEIGHT]
    against = [tag for weight, tag in parts[::-1][:MAX_REASONS] if weight <= -MIN_REASON_WEIGHT]
    return score, because, against


def recommended_count(total):
    '''打了分的 total 部影片里算"推荐"的部数：分数最高的 30%，不足 20 部时取 20 部，总共不到 20 部就全部'''
    return min(total, max(MIN_RECOMMENDED, (total * RECOMMENDED_PERCENT + 50) // 100))


def _recommend(model):
    scores = [db.Score(fanhao, *score_tags(model, tags)) for fanhao, tags in prepare.load_unlabeled()]
    result = db.save_scores(scores, recommended_count)
    if result.scored:
        logger.info(f'打分完成：给 {result.scored} 部没有用户打标的影片打了分，推荐 {result.recommended} 部，'
                    f'不太像 {result.scored - result.recommended} 部')
    else:
        logger.info('没有需要打分的影片')
    if result.skipped:
        logger.info(f'跳过 {result.skipped} 部（打分期间已经被打标或删除）')
    return result


def recommend():
    '''
    用保存的模型给所有还不是用户打标的影片打分（没有评分的，和只有系统评分的），分数入库，系统评分按分数重新划分：
    分数最高的一部分是"推荐"，其余是"不太像"。用户亲手打的标不动。返回 db.ScoreResult

    Raises:
        FileNotFoundError: 还没有训练过模型
        ModelLoadError: 模型文件读不了、内容不对，或者是旧版本程序训练的，需要重新训练；这时什么都不写
    '''
    with _model_lock:
        return _recommend(load())


def _smaller_class_recall(report):
    '''交叉验证里，打标少的那一类认对了多少'''
    likes_are_fewer = prepare.likes_are_fewer(report.likes, report.dislikes)
    return report.like_recall if likes_are_fewer else report.dislike_recall


def scored_sides(report):
    '''(打过分的影片数, 其中判到打标多的那一边的部数)。分数过半算判成"喜欢"'''
    scored, like_side = db.count_scores(0.5)
    likes_are_fewer = prepare.likes_are_fewer(report.likes, report.dislikes)
    return scored, (scored - like_side) if likes_are_fewer else like_side


def _nearly_all_on_one_side(report):
    scored, bigger_side = scored_sides(report)
    return scored >= MIN_SCORED_TO_JUDGE and bigger_side > scored * GUESSING_SHARE


def same_as_guessing(report):
    '''report（persist.Report）这个模型是不是和"所有影片都猜成打标多的那一类"没有差别，起不到筛选的作用'''
    recall = _smaller_class_recall(report)
    return recall < GUESSING_RECALL or (recall < SOUND_RECALL and _nearly_all_on_one_side(report))


def all_alike(report):
    '''
    检验结果说明模型分得开，而打过分的影片几乎都判到了打标多的那一边：这些影片本来就都像那一类，
    排序仍然有用
    '''
    return _smaller_class_recall(report) >= SOUND_RECALL and _nearly_all_on_one_side(report)


def train_and_recommend():
    '''
    训练，保存成功后立刻用新模型给影片重新打分，不等下一次抓取。返回 (SavedModel, db.ScoreResult)。
    出错的情况和 train 一样，这时不打分。
    模型保存以后，打分等不到数据库的写锁时不算训练失败：新模型照常生效，返回的打分结果是 None，
    分数留给下一次打分
    '''
    with _model_lock:
        saved = _train()
        try:
            return saved, _recommend(saved)
        except OperationalError as ex:
            if not db.is_busy(ex):
                raise
            logger.warning(f'模型已保存，但{db.DATABASE_BUSY}，影片还没有重新打分：下一次抓取后会自动打分')
            return saved, None
