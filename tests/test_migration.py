'''
演员数据的迁移：旧的存法（av-wiki 补名时每个名字各是一条 star 标签，地址是女优页）换成一个人一条正式名字 + 别名表。
升级后第一次启动时自动做：先做快照，再在一个事务里做完，做过有记号；离线能定的先定，定不准的留给后台校正
'''
import datetime
import os
import sqlite3

import pytest

from bustag import util
from bustag.spider import actresses, db
from bustag.spider.db import RATE_TYPE, RATE_VALUE, ItemRate

NOW = datetime.datetime(2026, 11, 11, 4, 0, 0)
AV = '/av-actress/'


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(actresses, 'get_now_time', lambda: NOW)
    monkeypatch.setattr(actresses, 'last_migration', None)


@pytest.fixture
def filled(cast):
    '''
    一部按旧的存法补过名字的影片：blocks 是 (slug, [名字…])，一位女优一段，按顺序写成 star 标签（名字已经有标签时共用那一条）。
    rate：用户打的标（补名时是喜欢，后来可能改成了不喜欢），None 是没有评分；lookup：有补名记录（补名记录表出现以前补的没有）
    '''
    def make(fanhao, *blocks, rate=RATE_VALUE.LIKE, lookup=True):
        cast.movie(fanhao)
        if rate is not None:
            ItemRate.create(item=fanhao, rate_type=RATE_TYPE.USER_RATE, rate_value=rate)
        for slug, names in blocks:
            cast.legacy(fanhao, slug, *names)
        if lookup:
            db.save_lookup(fanhao, db.LOOKUP_FILLED, 0, NOW, NOW)
    return make


def actress_of(page):
    '''这个女优页的女优：(正式名字, [别名…], JavBus 演员页, 资料读到哪一步)'''
    actress = db.Actress.get(db.Actress.avwiki == f'{AV}{page}/')
    aliases = [row.name for row in db.ActressAlias.select().where(db.ActressAlias.actress == actress.id)
               .order_by(db.ActressAlias.id)]
    return db.Tag.get_by_id(actress.tag_id).value, aliases, actress.javbus, actress.state


def star_values():
    return sorted(value for (value,) in db.Tag.select(db.Tag.value).where(db.Tag.type_ == 'star').tuples())


def lookup_status(fanhao):
    row = db.ActressLookup.get_or_none(db.ActressLookup.item == fanhao)
    return None if row is None else row.status


def snapshot_path():
    return os.path.join(util.get_data_dir(), 'bus-before-actress-upgrade-20261111.db')


class TestNothingToDo:
    def test_a_library_without_old_rows_is_left_alone(self, cast):
        cast.movie('C-001', ('演员甲', '/star/a1'))
        result = actresses.migrate()
        assert result.state == 'skipped'
        assert db.JobState.select().count() == 0
        assert not os.path.exists(snapshot_path())
        assert actresses.last_migration is result

    def test_a_migrated_library_is_not_migrated_again(self, filled):
        filled('L-001', ('jia', ['演员甲', 'えんいんこう']))
        assert actresses.migrate().state == 'done'
        before = (star_values(), db.ActressAlias.select().count(), db.Actress.select().count())
        os.remove(snapshot_path())
        assert actresses.migrate().state == 'skipped'
        assert (star_values(), db.ActressAlias.select().count(), db.Actress.select().count()) == before
        assert not os.path.exists(snapshot_path())

    def test_running_it_again_without_the_mark_changes_nothing(self, filled):
        filled('L-001', ('jia', ['演员甲', 'えんいんこう']))
        actresses.migrate()
        before = (star_values(), db.ActressAlias.select().count(), db.Actress.select().count(),
                  list(db.ItemTag.select(db.ItemTag.item, db.ItemTag.tag).tuples()))
        db.JobState.delete().execute()
        assert actresses.migrate().state == 'skipped'
        assert (star_values(), db.ActressAlias.select().count(), db.Actress.select().count(),
                list(db.ItemTag.select(db.ItemTag.item, db.ItemTag.tag).tuples())) == before


class TestOnePerson:
    def test_the_names_of_one_page_become_one_actress_with_aliases(self, filled, cast):
        filled('L-001', ('jia', ['演员甲', 'えんいんこう', 'Actor A']))
        filled('L-002', ('jia', ['演员甲', 'えんいんこう', 'Actor A']))
        result = actresses.migrate()
        assert (result.state, result.actresses, result.movies) == ('done', 1, 2)
        assert actress_of('jia') == ('演员甲', ['えんいんこう', 'Actor A'], '', db.ACTRESS_LEGACY)
        assert cast.stars_of('L-001') == ['演员甲'] and cast.stars_of('L-002') == ['演员甲']
        # 别名不再是标签
        assert star_values() == ['演员甲']
        assert lookup_status('L-001') == lookup_status('L-002') == db.LOOKUP_STALE
        mark = db.JobState.get(db.JobState.name == actresses.STORAGE_MARK)
        assert mark.at == NOW
        assert os.path.exists(snapshot_path())

    def test_a_javbus_star_among_her_names_becomes_her_name(self, filled, cast):
        cast.movie('C-001', ('演员甲', '/star/a1'))
        filled('L-001', ('jia', ['えんいんこう', '演员甲', 'Actor A']))
        actresses.migrate()
        assert actress_of('jia') == ('演员甲', ['えんいんこう', 'Actor A'], '/star/a1', db.ACTRESS_LEGACY)
        assert cast.stars_of('L-001') == ['演员甲']
        assert cast.stars_of('C-001') == ['演员甲']
        assert db.Tag.get(db.Tag.value == '演员甲').url == '/star/a1'

    def test_a_javbus_page_the_user_said_is_not_her_is_not_used(self, filled, cast):
        # 还没迁移的库里用户也能取消关联：那个演员页的标签即使和她的一个名字相同，也不是她
        cast.movie('C-001', ('演员甲', '/star/a1'))
        filled('L-001', ('jia', ['えんいんこう', '演员甲', 'Actor A']))
        actresses.unlink_javbus(f'{AV}jia/', '/star/a1')
        actresses.migrate()
        assert actress_of('jia') == ('えんいんこう', ['演员甲', 'Actor A'], '', db.ACTRESS_LEGACY)
        assert cast.stars_of('L-001') == ['えんいんこう'] and cast.stars_of('C-001') == ['演员甲']

    def test_the_javbus_star_that_comes_first_is_her_name(self, filled, cast):
        # 一位女优的两个名字对上了两条不同的 JavBus 标签：用排在前面的那条，另一条仍是 JavBus 自己的标签
        cast.movie('C-001', ('旧名甲', '/star/a2'), ('演员甲', '/star/a1'))
        cast.movie('C-002', ('旧名甲', '/star/a2'))
        filled('L-001', ('jia', ['演员甲', 'えんいんこう', '旧名甲']))
        actresses.migrate()
        assert actress_of('jia') == ('演员甲', ['えんいんこう', '旧名甲'], '/star/a1', db.ACTRESS_LEGACY)
        assert cast.stars_of('L-001') == ['演员甲']
        assert cast.stars_of('C-001') == ['旧名甲', '演员甲'] and cast.stars_of('C-002') == ['旧名甲']

    def test_a_movie_javbus_gave_keeps_its_stars(self, filled, cast):
        # JavBus 给的名字和补名建的那一行原文相同，就共用了那一行（地址是女优页）：这部影片的演员不是补名写的
        filled('L-001', ('jia', ['演员甲', 'えんいんこう', 'Actor A']))
        cast.movie('C-003', ('演员丙', '/star/c1'), ('演员甲', '/star/a9'))
        ItemRate.create(item='C-003', rate_type=RATE_TYPE.SYSTEM_RATE, rate_value=RATE_VALUE.DISLIKE)
        actresses.migrate()
        assert cast.stars_of('C-003') == ['演员丙', '演员甲']
        assert lookup_status('C-003') is None
        # 它就是 JavBus 给这位女优的名字
        assert actress_of('jia')[0] == '演员甲'

    def test_an_old_fill_without_a_record_counts_when_whole_pages_are_on_it(self, filled, cast):
        filled('L-001', ('jia', ['演员甲', 'えんいんこう']), ('yi', ['演员乙', 'えんいんおつ']), lookup=False)
        actresses.migrate()
        assert cast.stars_of('L-001') == ['演员甲', '演员乙']
        assert lookup_status('L-001') == db.LOOKUP_STALE

    def test_without_a_record_one_row_of_a_page_is_not_enough(self, filled, cast):
        # JavBus 的多人影片里一位演员的名字碰巧和补名建的一行原文相同，看起来也是这样：分不出来，不动
        cast.movie('C-001', ('演员丁', '/star/d1'))
        filled('L-001', ('bing', ['演员丁', 'えんいんてい']), lookup=False)
        actresses.migrate()
        assert cast.stars_of('L-001') == ['演员丁', 'えんいんてい']
        assert lookup_status('L-001') is None
        assert actress_of('bing')[:2] == ('えんいんてい', [])

    def test_a_name_equal_to_a_tag_of_another_type_stays_apart(self, filled, cast, make_item):
        make_item('C-001', tags=[('發行商:', 'えんいんこう'), ('genre', '标签一')])
        filled('L-001', ('jia', ['演员甲', 'えんいんこう']))
        actresses.migrate()
        assert actress_of('jia')[:2] == ('演员甲', ['えんいんこう'])
        assert db.Tag.get((db.Tag.type_ == '發行商:') & (db.Tag.value == 'えんいんこう'))
        item = db.Item.get_by_fanhao('C-001')
        db.Item.get_tags_dict(item)
        assert dict(item.tags_dict) == {'發行商:': ['えんいんこう'], 'genre': ['标签一']}


class TestSeveralPeople:
    def test_names_between_two_actresses_are_left_as_they_are(self, filled, cast):
        cast.movie('C-001', ('演员丙', '/star/c1'))
        filled('L-001', ('jia', ['演员甲', 'えんいんこう']), ('none', ['演员丙']), ('yi', ['演员乙', 'えんいんおつ']))
        actresses.migrate()
        # 演员丙夹在两位中间，可能是前一位的最后一个名字，也可能是后一位的第一个：拿不准，不并
        assert cast.stars_of('L-001') == ['演员甲', '演员丙', '演员乙']
        assert actress_of('jia')[:2] == ('演员甲', ['えんいんこう'])
        assert actress_of('yi')[:2] == ('演员乙', ['えんいんおつ'])

    def test_a_name_between_two_actresses_known_from_another_movie_joins_her(self, filled, cast):
        cast.movie('C-001', ('演员丙', '/star/c1'))
        filled('L-001', ('yi', ['演员丙', '演员乙', 'えんいんおつ']))
        filled('L-002', ('jia', ['演员甲', 'えんいんこう']), ('yi', ['演员丙', '演员乙', 'えんいんおつ']))
        actresses.migrate()
        assert actress_of('yi')[:3] == ('演员丙', ['演员乙', 'えんいんおつ'], '/star/c1')
        assert cast.stars_of('L-001') == ['演员丙']
        assert cast.stars_of('L-002') == ['演员甲', '演员丙']

    def test_a_name_inside_her_names_is_hers(self, filled, cast):
        cast.movie('C-001', ('旧名乙', '/star/b9'))
        filled('L-001', ('jia', ['演员甲', 'えんいんこう']), ('yi', ['演员乙', '旧名乙', 'えんいんおつ']))
        actresses.migrate()
        assert cast.stars_of('L-001') == ['演员甲', '旧名乙']
        assert actress_of('yi')[:3] == ('旧名乙', ['演员乙', 'えんいんおつ'], '/star/b9')

    def test_a_name_two_actresses_share_stays_with_the_one_it_sits_with(self, filled, cast):
        filled('L-001', ('yi', ['演员乙', '共用读音', 'Actor B']))
        filled('L-002', ('jia', ['演员甲', '共用读音', 'えんいんこう']))
        actresses.migrate()
        # 共用读音这一行是演员乙的女优页建的，在 L-002 上它夹在演员甲的名字中间：是演员甲的
        assert cast.stars_of('L-002') == ['演员甲']
        assert cast.stars_of('L-001') == ['演员乙']
        assert actress_of('jia')[:2] == ('演员甲', ['共用读音', 'えんいんこう'])
        assert actress_of('yi')[:2] == ('演员乙', ['共用读音', 'Actor B'])

    def test_a_shared_name_between_two_actresses_whose_own_page_is_elsewhere_goes_to_nobody(self, filled, cast):
        # 共用读音这一行是演员丙的女优页建的。演员丙的名字在 L-001 上全都在，在 L-002 上只有这一行、又夹在演员甲和
        # 演员乙中间：不是演员丙，也说不准是左右哪一位；左右两位都已经在这部上了，这一行不挂任何人
        filled('L-001', ('bing', ['演员丙', '共用读音', 'Actor C']))
        filled('L-002', ('jia', ['演员甲', 'えんいんこう']), ('none', ['共用读音']), ('yi', ['演员乙', 'えんいんおつ']))
        result = actresses.migrate()
        assert cast.stars_of('L-002') == ['演员甲', '演员乙']
        assert cast.stars_of('L-001') == ['演员丙']
        assert actress_of('bing')[:2] == ('演员丙', ['共用读音', 'Actor C'])
        assert not db.Tag.select().where(db.Tag.value == '共用读音').exists()
        assert (result.actresses, result.movies) == (3, 2)

    def test_a_name_between_two_actresses_on_a_movie_where_nobody_else_is_sure_still_counts(self, filled, cast):
        # 一部影片上谁都不"在"（每个女优页都只有一小半的行在上面）时，旧行照旧算建它的那位：去掉的话这部就少了人
        filled('L-001', ('jia', ['演员甲', 'えんいんこう', 'Actor A']))
        filled('L-002', ('none', ['えんいんこう']))
        actresses.migrate()
        assert cast.stars_of('L-002') == ['演员甲']

    def test_two_actresses_with_the_same_first_name_are_not_merged(self, filled, cast):
        filled('L-001', ('jia', ['同名', 'えんいんこう']))
        filled('L-002', ('yi', ['同名', 'えんいんおつ']))
        actresses.migrate()
        assert actress_of('jia')[:2] == ('同名', ['えんいんこう'])
        assert actress_of('yi')[:2] == ('同名（2）', ['同名', 'えんいんおつ'])
        assert cast.stars_of('L-001') == ['同名'] and cast.stars_of('L-002') == ['同名（2）']

    def test_the_javbus_page_found_for_her_is_kept(self, filled, cast):
        filled('L-001', ('jia', ['演员甲', 'えんいんこう']))
        db.StarLink.create(key=f'avwiki:{AV}jia/', name='演员甲', state=db.LINK_FOUND, path='/star/a1')
        actresses.migrate()
        assert actress_of('jia')[2] == '/star/a1'


LATER = NOW + datetime.timedelta(hours=2)


def links_of(*fanhaos):
    return list(db.ItemTag.select(db.ItemTag.id, db.ItemTag.item, db.ItemTag.tag)
                .where(db.ItemTag.item.in_(fanhaos)).order_by(db.ItemTag.id).tuples())


def file_bytes(path):
    with open(path, 'rb') as f:
        return f.read()


class TestAfterADowngrade:
    '''迁移以后又用旧版本跑过：它按旧存法给"喜欢、没有演员"的影片写的名字，再启动新版本时整理掉'''

    @staticmethod
    def written_by_an_older_version(cast, fanhao, *blocks):
        '''旧版本补名：每个名字各是一条 star 标签（名字已经是一条标签时共用那一条），记下补到了'''
        cast.movie(fanhao)
        ItemRate.create(item=fanhao, rate_type=RATE_TYPE.USER_RATE, rate_value=RATE_VALUE.LIKE)
        for slug, names in blocks:
            cast.legacy(fanhao, slug, *names)
        db.save_lookup(fanhao, db.LOOKUP_FILLED, 0, LATER, LATER)

    @pytest.fixture
    def later(self, monkeypatch):
        def at(moment=LATER):
            monkeypatch.setattr(actresses, 'get_now_time', lambda: moment)
        return at

    def test_names_written_the_old_way_are_tidied_into_the_actresses_already_there(self, filled, cast, later):
        filled('L-001', ('jia', ['演员甲', 'えんいんこう']))
        cast.movie('C-001', ('演员丙', '/star/c1'))
        assert actresses.migrate().state == 'done'
        db.JobState.create(name=actresses.FIXED_MARK, text='done', at=NOW)
        tidy = links_of('L-001', 'C-001')
        self.written_by_an_older_version(cast, 'L-002', ('jia', ['演员甲', 'えんいんこう', 'Actor A']),
                                         ('yi', ['演员乙', 'えんいんおつ']))
        later()
        result = actresses.migrate()
        assert (result.state, result.again, result.actresses, result.movies) == ('done', True, 1, 1)
        assert cast.stars_of('L-002') == ['演员甲', '演员乙']
        # 已经有的那位并进她（别名补上），新出现的那位新建
        assert db.Actress.select().where(db.Actress.avwiki == f'{AV}jia/').count() == 1
        assert actress_of('jia')[:2] == ('演员甲', ['えんいんこう', 'Actor A'])
        assert actress_of('yi') == ('演员乙', ['えんいんおつ'], '', db.ACTRESS_LEGACY)
        assert star_values() == ['演员丙', '演员乙', '演员甲']
        # 已经整理好的影片一条不动；新整理的这一部后台按新存法再查一次
        assert links_of('L-001', 'C-001') == tidy
        assert (lookup_status('L-001'), lookup_status('L-002')) == (db.LOOKUP_STALE, db.LOOKUP_STALE)
        # 只整理了这几部，不算演员的名字整体换了一次：升级、校正做完的记号不动（模型不因此重新训练），另记这一次的时间
        marks = dict(db.JobState.select(db.JobState.name, db.JobState.at).tuples())
        assert (marks[actresses.STORAGE_MARK], marks[actresses.FIXED_MARK], marks[actresses.RETIDY_MARK]) == (
            NOW, NOW, LATER)
        assert actresses.features_changed_at() == NOW

    def test_a_newcomer_with_the_same_first_name_skips_a_number_kept_as_an_alias(self, filled, cast, later):
        filled('L-001', ('jia', ['同名', 'えんいんこう']))
        filled('L-002', ('yi', ['同名', 'えんいんおつ']))
        assert actresses.migrate().state == 'done'
        db.JobState.create(name=actresses.FIXED_MARK, text='done', at=NOW)
        # 以前的版本给第二位编过的号留在她的别名里，设定里可能存着它
        db.add_aliases(db.Actress.get(db.Actress.avwiki == f'{AV}yi/'), ['同名（3）'])
        self.written_by_an_older_version(cast, 'L-003', ('bing', ['同名', 'えんいんへい']))
        later()
        assert actresses.migrate().state == 'done'
        assert actress_of('yi')[0] == '同名（2）'
        assert actress_of('bing')[0] == '同名（4）'
        assert cast.stars_of('L-003') == ['同名（4）']

    def test_the_snapshot_of_the_first_upgrade_stays_and_this_one_gets_its_own_name(self, filled, cast, later):
        filled('L-001', ('jia', ['演员甲', 'えんいんこう']))
        actresses.migrate()
        first = file_bytes(snapshot_path())
        self.written_by_an_older_version(cast, 'L-002', ('jia', ['演员甲', 'Actor A']))
        later()
        result = actresses.migrate()
        assert result.snapshot == os.path.join(util.get_data_dir(), 'bus-before-actress-upgrade-20261111-060000.db')
        assert file_bytes(snapshot_path()) == first
        copy = sqlite3.connect(result.snapshot)
        try:
            assert copy.execute("SELECT COUNT(*) FROM item_tag JOIN tag ON tag.id = item_tag.tag_id "
                                "WHERE item_id = 'L-002' AND tag.type = 'star'").fetchone()[0] == 2
        finally:
            copy.close()

    @staticmethod
    def snapshots():
        return sorted(name for name in os.listdir(util.get_data_dir()) if name.startswith('bus-before'))

    @staticmethod
    def cut_off_before_saving(monkeypatch):
        '''整理做完、提交之前进程被结束：事务回滚，这一次的快照来不及删（换回原来的整理时调返回的函数）'''
        real = actresses._Rewrite.run

        def killed(rewrite):
            real(rewrite)
            raise KeyboardInterrupt

        monkeypatch.setattr(actresses._Rewrite, 'run', killed)
        return lambda: monkeypatch.setattr(actresses._Rewrite, 'run', real)

    def test_tidies_cut_off_before_saving_leave_no_extra_snapshots(self, filled, cast, later, monkeypatch):
        filled('L-001', ('jia', ['演员甲', 'えんいんこう']))
        actresses.migrate()
        first = file_bytes(snapshot_path())
        self.written_by_an_older_version(cast, 'L-002', ('jia', ['演员甲', 'Actor A']))
        restore = self.cut_off_before_saving(monkeypatch)
        for minutes in (0, 1):
            later(LATER + datetime.timedelta(minutes=minutes))
            with pytest.raises(KeyboardInterrupt):
                actresses.migrate()
        restore()
        later(LATER + datetime.timedelta(minutes=2))
        assert actresses.migrate().state == 'done'
        # 被打断的两次留下的不算数：只剩当天第一份和做成的这一份
        assert self.snapshots() == ['bus-before-actress-upgrade-20261111-060200.db',
                                    'bus-before-actress-upgrade-20261111.db']
        assert file_bytes(snapshot_path()) == first
        assert cast.stars_of('L-002') == ['演员甲']

    def test_the_snapshot_of_a_tidy_that_was_saved_stays(self, filled, cast, later, monkeypatch):
        filled('L-001', ('jia', ['演员甲', 'えんいんこう']))
        actresses.migrate()
        self.written_by_an_older_version(cast, 'L-002', ('jia', ['演员甲', 'Actor A']))
        later()
        saved = actresses.migrate().snapshot
        kept = file_bytes(saved)
        self.written_by_an_older_version(cast, 'L-003', ('jia', ['演员甲', 'Actor A']))
        restore = self.cut_off_before_saving(monkeypatch)
        later(LATER + datetime.timedelta(minutes=1))
        with pytest.raises(KeyboardInterrupt):
            actresses.migrate()
        restore()
        later(LATER + datetime.timedelta(minutes=2))
        assert actresses.migrate().state == 'done'
        assert self.snapshots() == ['bus-before-actress-upgrade-20261111-060000.db',
                                    'bus-before-actress-upgrade-20261111-060200.db',
                                    'bus-before-actress-upgrade-20261111.db']
        assert file_bytes(saved) == kept

    def test_a_temporary_file_left_by_a_cut_off_snapshot_is_cleared_too(self, filled, cast, later):
        filled('L-001', ('jia', ['演员甲', 'えんいんこう']))
        actresses.migrate()
        self.written_by_an_older_version(cast, 'L-002', ('jia', ['演员甲', 'Actor A']))
        folder = util.get_data_dir()
        for name in ('bus-before-actress-upgrade-20261111-055900.db.tmp',
                     'bus-before-actress-upgrade-20261111-055900.db.tmp-journal'):
            with open(os.path.join(folder, name), 'wb') as f:
                f.write(b'half written')
        later()
        assert actresses.migrate().state == 'done'
        assert self.snapshots() == ['bus-before-actress-upgrade-20261111-060000.db',
                                    'bus-before-actress-upgrade-20261111.db']

    def test_on_another_day_the_snapshot_takes_that_days_name(self, filled, cast, later):
        filled('L-001', ('jia', ['演员甲', 'えんいんこう']))
        actresses.migrate()
        first = file_bytes(snapshot_path())
        self.written_by_an_older_version(cast, 'L-002', ('jia', ['演员甲', 'Actor A']))
        later(NOW + datetime.timedelta(days=1))
        result = actresses.migrate()
        assert result.snapshot == os.path.join(util.get_data_dir(), 'bus-before-actress-upgrade-20261112.db')
        assert file_bytes(snapshot_path()) == first

    def test_old_rows_left_on_movies_the_fill_did_not_write_are_not_tidied_again(self, filled, cast, later):
        # 迁移以后合法留着的旧行（JavBus 的影片共用的、没有补名记录的影片上的）：再启动时不整理、不做快照
        filled('L-001', ('jia', ['演员甲', 'えんいんこう']))
        filled('L-004', ('ding', ['えんいんぼ', '演员戊']), rate=None, lookup=False)
        actresses.migrate()
        assert db.Tag.get(db.Tag.value == '演员戊').url == f'{AV}ding/'
        os.remove(snapshot_path())
        before = links_of('L-001', 'L-004')
        later()
        assert actresses.migrate().state == 'skipped'
        assert links_of('L-001', 'L-004') == before
        assert db.JobState.get(db.JobState.name == actresses.STORAGE_MARK).at == NOW
        assert not [name for name in os.listdir(util.get_data_dir()) if name.startswith('bus-before')]

    def test_a_tidy_that_fails_leaves_no_snapshot_and_is_done_next_time(self, filled, cast, later, locked_database):
        filled('L-001', ('jia', ['演员甲', 'えんいんこう']))
        actresses.migrate()
        first = file_bytes(snapshot_path())
        self.written_by_an_older_version(cast, 'L-002', ('jia', ['演员甲', 'Actor A']))
        later()
        with locked_database():
            result = actresses.migrate()
        assert (result.state, result.again) == ('failed', True)
        assert result.reason.startswith('数据库正被别的程序占用')
        assert sorted(name for name in os.listdir(util.get_data_dir()) if name.startswith('bus-before')) == [
            'bus-before-actress-upgrade-20261111.db']
        assert file_bytes(snapshot_path()) == first
        assert cast.stars_of('L-002') == ['演员甲', 'Actor A']
        assert actresses.migrate().state == 'done'
        assert cast.stars_of('L-002') == ['演员甲']


class TestHowLongItWaited:
    '''升级没做成时说的等了多久，是按钟表量的这一次（SQLite 自己的等待按名义时长累计，机器忙时实际常是两倍多）'''

    def test_the_reason_gives_the_seconds_it_really_waited(self, filled, monkeypatch, locked_database):
        filled('L-001', ('jia', ['演员甲', 'えんいんこう']))
        clock = iter([100.0, 200.0, 262.4])
        monkeypatch.setattr(actresses, 'monotonic', lambda: next(clock))
        with locked_database():
            result = actresses.migrate()
        assert (result.state, result.reason) == ('failed', '数据库正被别的程序占用（等了 62 秒还没轮到）')

    def test_less_than_a_second(self, filled, monkeypatch, locked_database):
        filled('L-001', ('jia', ['演员甲', 'えんいんこう']))
        clock = iter([100.0, 200.0, 200.3])
        monkeypatch.setattr(actresses, 'monotonic', lambda: next(clock))
        with locked_database():
            assert actresses.migrate().reason == '数据库正被别的程序占用（等了不到 1 秒还没轮到）'


class TestSafety:
    def library(self, filled, cast, make_item):
        cast.movie('C-001', ('演员丙', '/star/c1'), ('演员丁', '/star/d1'))
        cast.movie('C-002', ('演员丙', '/star/c1'))
        filled('L-001', ('jia', ['演员甲', 'えんいんこう', 'Actor A']), ('yi', ['演员乙', '演员丙', 'えんいんおつ']))
        filled('L-002', ('jia', ['演员甲', 'えんいんこう', 'Actor A']))
        filled('L-003', ('bing', ['演员丁', 'えんいんてい', 'Actress D']), rate=RATE_VALUE.DISLIKE, lookup=False)
        filled('L-004', ('ding', ['えんいんぼ', '演员戊']), rate=None, lookup=False)
        make_item('M-001', tags=[('genre', '标签一'), ('系列:', '系列甲')])
        ItemRate.create(item='M-001', rate_type=RATE_TYPE.USER_RATE, rate_value=RATE_VALUE.DISLIKE,
                        rete_time=NOW - datetime.timedelta(days=3))

    def everything(self):
        tags = list(db.Tag.select(db.Tag.id, db.Tag.type_, db.Tag.value, db.Tag.url).order_by(db.Tag.id).tuples())
        links = list(db.ItemTag.select(db.ItemTag.item, db.ItemTag.tag).order_by(db.ItemTag.id).tuples())
        rates = list(ItemRate.select(ItemRate.item, ItemRate.rate_type, ItemRate.rate_value, ItemRate.rete_time)
                     .order_by(ItemRate.id).tuples())
        items = list(db.Item.select(db.Item.fanhao).order_by(db.Item.id).tuples())
        return tags, links, rates, items

    def test_what_must_not_change(self, filled, cast, make_item):
        self.library(filled, cast, make_item)
        tags, links, rates, items = self.everything()
        actresses.migrate()
        tags2, links2, rates2, items2 = self.everything()
        assert items2 == items and rates2 == rates
        not_star = {tag for tag in tags if tag[1] != 'star'}
        assert not_star == {tag for tag in tags2 if tag[1] != 'star'}
        stars = {tag[0] for tag in tags if tag[1] == 'star'}
        assert [link for link in links if link[1] not in stars] == [link for link in links2 if link[1] not in stars]
        javbus = {tag for tag in tags if tag[1] == 'star' and not tag[3].startswith(AV)}
        assert javbus <= set(tags2)
        # 演员不是补名写的影片，挂接一条不变
        untouched = ('C-001', 'C-002', 'L-004', 'M-001')
        assert [link for link in links if link[0] in untouched] == [link for link in links2 if link[0] in untouched]
        # 没有挂着已删标签的影片，没有指向不存在的女优的别名
        ids = {tag[0] for tag in tags2}
        assert all(tag in ids for _, tag in links2)
        actress_ids = {actress.id for actress in db.Actress.select()}
        assert all(alias.actress_id in actress_ids for alias in db.ActressAlias.select())
        assert all(actress.tag_id in ids for actress in db.Actress.select())

    def test_every_movie_keeps_each_of_its_actresses(self, filled, cast, make_item):
        self.library(filled, cast, make_item)
        actresses.migrate()
        assert cast.stars_of('L-001') == ['演员甲', '演员丙']
        assert cast.stars_of('L-002') == ['演员甲']
        # 补名时喜欢、后来改成不喜欢的，也是补名写的
        assert cast.stars_of('L-003') == ['演员丁']
        assert actress_of('yi')[:3] == ('演员丙', ['演员乙', 'えんいんおつ'], '/star/c1')
        assert actress_of('bing')[:3] == ('演员丁', ['えんいんてい', 'Actress D'], '/star/d1')
        # 没有用户打标的影片不会是补名写的（补名只补用户喜欢的）：不动，那一个女优页照样整理成一位女优
        assert cast.stars_of('L-004') == ['えんいんぼ', '演员戊']
        assert actress_of('ding')[:2] == ('えんいんぼ', ['演员戊'])
        assert lookup_status('L-004') is None

    def test_the_snapshot_is_a_copy_from_before(self, filled, cast, make_item):
        self.library(filled, cast, make_item)
        tags, links, _, _ = self.everything()
        actresses.migrate()
        copy = sqlite3.connect(snapshot_path())
        try:
            assert copy.execute('SELECT COUNT(*) FROM tag').fetchone()[0] == len(tags)
            assert copy.execute('SELECT COUNT(*) FROM item_tag').fetchone()[0] == len(links)
        finally:
            copy.close()

    def test_a_snapshot_left_by_an_earlier_try_the_same_day_is_replaced(self, filled, cast, make_item):
        self.library(filled, cast, make_item)
        with open(snapshot_path(), 'wb') as f:
            f.write(b'half written')
        assert actresses.migrate().state == 'done'
        copy = sqlite3.connect(snapshot_path())
        try:
            assert copy.execute('SELECT COUNT(*) FROM item').fetchone()[0] == 7
        finally:
            copy.close()

    def test_no_snapshot_no_migration(self, filled, cast, make_item, monkeypatch, caplog):
        self.library(filled, cast, make_item)
        before = self.everything()

        def disk_full(path):
            raise OSError(28, '磁盘空间不足')

        monkeypatch.setattr(actresses, '_snapshot', disk_full)
        result = actresses.migrate()
        assert result.state == 'failed'
        assert result.reason == '升级前的快照没有做成（磁盘空间不足）'
        assert self.everything() == before
        assert db.JobState.select().count() == 0 and db.Actress.select().count() == 0
        assert any('演员数据没有升级' in message for message in caplog.messages)

    def test_a_database_held_by_another_program_is_left_as_it_was(self, filled, cast, make_item, locked_database):
        self.library(filled, cast, make_item)
        before = self.everything()
        with locked_database():
            result = actresses.migrate()
        assert result.state == 'failed'
        assert result.reason.startswith('数据库正被别的程序占用')
        assert self.everything() == before
        assert db.Actress.select().count() == 0
        # 库没有变，这一次的快照留着没用：删掉（程序开着时每天再试，不攒一堆快照）
        assert not os.path.exists(snapshot_path())

    def test_an_old_database_held_exclusively_is_named_as_such(self, filled, cast, make_item, monkeypatch):
        self.library(filled, cast, make_item)

        def held():
            raise db.LockedOut('database is locked (another program holds it exclusively)')

        monkeypatch.setattr(actresses, 'migrated', held)
        result = actresses.migrate()
        assert (result.state, result.reason) == ('failed', db.DATABASE_LOCKED_OUT)
        assert not os.path.exists(snapshot_path())

    def test_an_error_halfway_leaves_everything_as_it_was(self, filled, cast, make_item, monkeypatch):
        self.library(filled, cast, make_item)
        before = self.everything()

        def broken(actress, names):
            raise RuntimeError('意料之外的错')

        monkeypatch.setattr(db, 'add_aliases', broken)
        result = actresses.migrate()
        assert result.state == 'failed' and 'RuntimeError' in result.reason
        assert self.everything() == before
        assert db.Actress.select().count() == 0 and db.ActressAlias.select().count() == 0
        assert db.JobState.select().count() == 0
        assert not os.path.exists(snapshot_path())

    def test_the_tables_keep_their_columns(self, filled, cast, make_item):
        self.library(filled, cast, make_item)
        statements = sorted(sql for (sql,) in db.db.execute_sql(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall())
        actresses.migrate()
        assert sorted(sql for (sql,) in db.db.execute_sql(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall()) == statements

    def test_the_log_has_only_numbers(self, filled, cast, make_item, caplog):
        import logging
        caplog.set_level(logging.DEBUG, logger='bustag')
        self.library(filled, cast, make_item)
        actresses.migrate()
        for name in ('演员甲', '演员乙', '演员丙', '演员丁', 'えんいん', 'Actor', '/jia/', '/yi/', '/bing/'):
            assert name not in caplog.text
        assert '演员数据升级完成' in caplog.text
