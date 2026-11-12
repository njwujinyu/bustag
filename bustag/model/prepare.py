'''
训练和打分用的数据：影片的标签值，用户的打标，以及打标够不够训练
'''
import math
from collections import defaultdict

from peewee import JOIN, fn

from bustag.spider.db import RATE_TYPE, RATE_VALUE, Item, ItemRate, ItemTag, Tag

# 用户打标至少这么多条才训练
MIN_TRAIN_NUM = 200
# "喜欢""不喜欢"里少的那一类至少这么多条才训练：再少的话模型学不到这一类，检验结果也没有意义
MIN_MINORITY = 10
# 少的那一类不到这么多条时照常训练，但检验结果的误差很大，只能参考
SOLID_MINORITY = 30
# 建议把少的那一类攒到这么多条：到这时"认对几成"的误差从二十多个百分点降到十几个
TARGET_MINORITY = 50
USER_RATED = ((ItemRate.rate_type == RATE_TYPE.USER_RATE)
              & ItemRate.rate_value.in_([RATE_VALUE.LIKE, RATE_VALUE.DISLIKE]))
NOT_USER_RATED = ItemRate.rate_type.is_null() | (ItemRate.rate_type == RATE_TYPE.SYSTEM_RATE)


def _movies(where):
    '''
    符合条件的影片：[(番号, 评分的值, 标签值的集合)]，新入库的在前。where 里可以用 Item 和 ItemRate 的列。
    标签不分类型（类别、演员、系列、製作商、發行商、導演混在一起），不同类型里相同的值算一个
    '''
    movies = (Item.select(Item.fanhao, ItemRate.rate_value)
              .join(ItemRate, JOIN.LEFT_OUTER)
              .where(where)
              .order_by(Item.id.desc()))
    carried = (ItemTag.select(ItemTag.item, Tag.value)
               .join(Tag)
               .where(ItemTag.item.in_(movies.clone().select(Item.fanhao).order_by())))
    tags = defaultdict(set)
    for fanhao, value in carried.tuples():
        tags[fanhao].add(value)
    return [(fanhao, rate_value, tags[fanhao]) for fanhao, rate_value in movies.tuples()]


def load_labeled():
    '''用户亲手打过标的影片：[(标签值的集合, 1 喜欢 / 0 不喜欢)]'''
    return [(tags, rate_value) for _, rate_value, tags in _movies(USER_RATED)]


def load_unlabeled():
    '''还不是用户打标的影片（没有评分的，和只有系统评分的）：[(番号, 标签值的集合)]，新入库的在前'''
    return [(fanhao, tags) for fanhao, _, tags in _movies(NOT_USER_RATED)]


def has_unlabeled():
    '''库里有没有还不是用户打标的影片（没有评分的，和只有系统评分的）'''
    return Item.select(Item.id).join(ItemRate, JOIN.LEFT_OUTER).where(NOT_USER_RATED).exists()


def count_labels():
    '''用户打标的条数：(喜欢, 不喜欢)'''
    counts = dict(ItemRate.select(ItemRate.rate_value, fn.COUNT(ItemRate.id))
                  .where(USER_RATED)
                  .group_by(ItemRate.rate_value)
                  .tuples())
    return counts.get(RATE_VALUE.LIKE, 0), counts.get(RATE_VALUE.DISLIKE, 0)


def likes_are_fewer(likes, dislikes):
    '''打标少的那一类是不是"喜欢"；一样多时算"不喜欢"少'''
    return likes < dislikes


def smaller_class(likes, dislikes):
    '''打标少的那一类：(叫法, 条数)'''
    return ('喜欢', likes) if likes_are_fewer(likes, dislikes) else ('不喜欢', dislikes)


def training_problem(likes, dislikes):
    '''打标数据还不够训练时返回原因（中文，可以直接显示给用户），够了返回 None'''
    total = likes + dislikes
    if total < MIN_TRAIN_NUM:
        return f'训练数据不足, 无法训练模型. 需要{MIN_TRAIN_NUM}, 当前{total}'
    name, count = smaller_class(likes, dislikes)
    if count < MIN_MINORITY:
        return (f'"{name}"的打标只有 {count} 条，至少要 {MIN_MINORITY} 条才能训练，还差 {MIN_MINORITY - count} 条。'
                f'请再标一些"{name}"的影片')
    return None


def for_reference_only(likes, dislikes):
    '''少的那一类不到 SOLID_MINORITY 条：模型的结果只能参考'''
    return smaller_class(likes, dislikes)[1] < SOLID_MINORITY


def recall_margin(count):
    '''
    某一类只有 count 条打标时，交叉验证得到的"这一类认对了几成"上下可能差多少个百分点
    （95% 的把握，按比例正好一半这种最坏的情况算，所以不会因为比例接近 0 或 100% 而显得很准）
    '''
    return round(100 * 1.96 * math.sqrt(0.25 / count))
