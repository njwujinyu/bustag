'''
女优的存法：一个人一条正式名字（star 标签）+ 别名表 + 两个地址。按名字找人、一条标签算谁；
还没迁移的库（av-wiki 补的每个名字各是一条 star 标签）也认得
'''
import datetime
import threading

import pytest
from peewee import OperationalError

from bustag.spider import actresses, db

AV = '/av-actress/'
DAY = datetime.datetime(2026, 11, 11, 12, 0, 0)


def names_of(people):
    return [(person.name, list(person.aliases)) for person in people]


def tag_id(value):
    return db.Tag.get((db.Tag.type_ == 'star') & (db.Tag.value == value)).id


class TestFindPeople:
    def test_any_name_of_an_actress_finds_her(self, cast):
        jia = cast.actress('演员甲', 'jia', 'えんいんこう', 'Actor A')
        cast.actress('演员乙', 'yi', 'えんいんおつ')
        for name in ('演员甲', 'えんいんこう', 'Actor A', 'actor a', ' ACTOR A ', '演員甲'):
            people = actresses.find_people([name])[name]
            assert names_of(people) == [('演员甲', ['えんいんこう', 'Actor A'])], name
            person, = people
            assert (person.key, person.actress, person.avwiki, person.javbus) == (
                f'actress:{jia.id}', jia.id, f'{AV}jia/', '')
            assert person.tags == {tag_id('演员甲')}

    def test_a_name_two_actresses_share_finds_both(self, cast):
        cast.actress('演员甲', 'jia', '共用读音')
        cast.actress('演员乙', 'yi', '共用读音', 'Actor B')
        found = actresses.find_people(['共用读音', '演员乙'])
        assert names_of(found['共用读音']) == [('演员甲', ['共用读音']), ('演员乙', ['共用读音', 'Actor B'])]
        assert names_of(found['演员乙']) == [('演员乙', ['共用读音', 'Actor B'])]

    def test_an_alias_that_is_another_actresses_name_finds_both(self, cast):
        cast.actress('演员甲', 'jia')
        cast.actress('演员乙', 'yi', '演员甲')
        assert names_of(actresses.find_people(['演员甲'])['演员甲']) == [('演员甲', []), ('演员乙', ['演员甲'])]

    def test_a_star_from_javbus_is_a_person_of_her_own(self, cast):
        cast.movie('C-001', ('演员丙', '/star/c1'))
        person, = actresses.find_people(['演员丙'])['演员丙']
        assert (person.key, person.name, person.aliases, person.javbus, person.avwiki, person.actress) == (
            'javbus:/star/c1', '演员丙', (), '/star/c1', '', None)
        assert person.tags == {tag_id('演员丙')}

    def test_the_names_on_one_javbus_page_are_one_person_named_by_the_newest(self, cast):
        # 网站给她改过名：两条 star 标签，地址相同
        cast.movie('C-001', ('旧名丙', '/star/c1'))
        cast.movie('C-002', ('演员丙', '/star/c1'))
        for name in ('旧名丙', '演员丙'):
            person, = actresses.find_people([name])[name]
            assert (person.key, person.name, person.aliases) == ('javbus:/star/c1', '演员丙', ('旧名丙',))
            assert person.tags == {tag_id('旧名丙'), tag_id('演员丙')}

    def test_a_star_with_a_made_up_link_is_only_itself(self, make_item):
        make_item('C-001', tags=[('star', '演员丁')])
        person, = actresses.find_people(['演员丁'])['演员丁']
        assert (person.key, person.name, person.javbus, person.avwiki) == (f'tag:{tag_id("演员丁")}', '演员丁', '', '')

    def test_a_star_needs_the_star_type(self, make_item):
        make_item('C-001', tags=[('genre', '演员甲'), ('系列:', '演员甲')])
        assert actresses.find_people(['演员甲']) == {'演员甲': []}

    def test_names_nobody_has(self, cast):
        cast.actress('演员甲', 'jia', 'えんいんこう')
        assert actresses.find_people(['没有的名字', '演员']) == {'没有的名字': [], '演员': []}

    def test_a_javbus_star_on_her_javbus_page_counts_as_her(self, cast):
        jia = cast.actress('演员甲', 'jia', 'えんいんこう', javbus='/star/a1')
        cast.movie('C-001', ('別の名前', '/star/a1'))
        for name in ('演员甲', 'えんいんこう', '別の名前'):
            person, = actresses.find_people([name])[name]
            assert (person.key, person.name, person.javbus) == (f'actress:{jia.id}', '演员甲', '/star/a1'), name
            assert person.tags == {tag_id('演员甲'), tag_id('別の名前')}

    def test_a_javbus_star_as_her_name_gives_her_javbus_page(self, cast):
        # 正式名字就是 JavBus 的那条标签
        cast.movie('C-001', ('演员甲', '/star/a1'))
        jia = cast.actress('演员甲', 'jia', 'えんいんこう')
        person, = actresses.find_people(['えんいんこう'])['えんいんこう']
        assert (person.key, person.javbus, person.avwiki) == (f'actress:{jia.id}', '/star/a1', f'{AV}jia/')

    def test_a_row_left_with_her_page_address_counts_as_her(self, cast):
        # 迁移时她的正式名字换成了 JavBus 的那条，JavBus 影片挂着的旧行（地址是她的女优页）留着
        cast.movie('C-001', ('演员甲', '/star/a1'))
        cast.movie('C-002', ('旧行甲', f'{AV}jia/'))
        jia = cast.actress('演员甲', 'jia', '旧行甲')
        for name in ('演员甲', '旧行甲'):
            person, = actresses.find_people([name])[name]
            assert person.key == f'actress:{jia.id}'
            assert person.tags == {tag_id('演员甲'), tag_id('旧行甲')}

    def test_blanks_around_a_stored_name_do_not_matter(self, cast):
        cast.movie('C-001', ('\u3000演员丙 ', '/star/c1'))
        assert names_of(actresses.find_people(['演员丙'])['演员丙']) == [('\u3000演员丙 ', [])]

    def test_long_names_written_in_letters(self, cast):
        # 英文字母的大小写组合太多，不能一一列出时换一种比法
        name = 'Very Long Name Written In Letters'
        cast.movie('C-001', (name, '/star/c1'))
        assert names_of(actresses.find_people([name.upper()])[name.upper()]) == [(name, [])]

    def test_stored_names_are_compared_inside_sqlite(self, cast, sql_folds):
        cast.actress('演員甲', 'jia', 'えんいんこう')
        for n in range(30):
            cast.movie(f'C-{n:03d}', (f'演員{n}', f'/star/s{n}'))
        found = actresses.find_people(['演员甲', 'エンインコウ', '演员7', '  演员7  '])
        assert [len(found[name]) for name in found] == [1, 0, 1, 1]
        # 库里的名字没有一行一行从 SQLite 调回 Python 去折：几个请求同时这样做会互相等解释器锁
        assert sql_folds == []


class TestUnmigratedRows:
    '''迁移前的库：av-wiki 补的名字每个是一条 star 标签，地址是女优页；同一个女优页的算一个人'''

    def test_the_rows_of_one_page_are_one_person_named_by_the_first_row(self, cast):
        cast.movie('L-001')
        cast.legacy('L-001', 'jia', '演员甲', 'えんいんこう', 'Actor A')
        for name in ('演员甲', 'えんいんこう', 'actor a'):
            person, = actresses.find_people([name])[name]
            assert (person.key, person.name, person.aliases, person.avwiki, person.actress) == (
                f'avwiki:{AV}jia/', '演员甲', ('えんいんこう', 'Actor A'), f'{AV}jia/', None)
            assert person.tags == {tag_id(value) for value in ('演员甲', 'えんいんこう', 'Actor A')}

    def test_a_row_shared_with_javbus_keeps_its_javbus_address(self, cast):
        cast.movie('C-001', ('演员甲', '/star/a1'))
        cast.movie('L-001')
        cast.legacy('L-001', 'jia', '演员甲', 'えんいんこう')
        assert names_of(actresses.find_people(['演员甲'])['演员甲']) == [('演员甲', [])]
        assert names_of(actresses.find_people(['えんいんこう'])['えんいんこう']) == [('えんいんこう', [])]

    def test_the_javbus_page_found_for_her_joins_in(self, cast):
        cast.movie('L-001')
        cast.legacy('L-001', 'jia', 'えんいんこう', 'Actor A')
        cast.movie('C-001', ('演员甲', '/star/a1'))
        db.StarLink.create(key=f'avwiki:{AV}jia/', name='えんいんこう', state=db.LINK_FOUND, path='/star/a1')
        person, = actresses.find_people(['Actor A'])['Actor A']
        assert person.javbus == '/star/a1'
        assert person.tags == {tag_id(value) for value in ('えんいんこう', 'Actor A', '演员甲')}


class TestFoundPages:
    def test_a_name_not_in_the_library_takes_the_page_found_for_it(self, cast):
        cast.movie('C-001', ('演员甲', '/star/a1'))
        db.StarLink.create(key='name:另一个写法', name='另一个写法', state=db.LINK_FOUND, path='/star/a1')
        person, = actresses.find_people(['另一个写法'])['另一个写法']
        assert (person.key, person.name, person.javbus) == ('javbus:/star/a1', '演员甲', '/star/a1')

    def test_a_page_found_for_an_actress_without_one_is_hers(self, cast):
        jia = cast.actress('演员甲', 'jia', 'えんいんこう')
        cast.movie('C-001', ('別の名前', '/star/a1'))
        db.StarLink.create(key=f'avwiki:{AV}jia/', name='演员甲', state=db.LINK_FOUND, path='/star/a1')
        person, = actresses.find_people(['えんいんこう'])['えんいんこう']
        assert (person.key, person.javbus) == (f'actress:{jia.id}', '/star/a1')
        assert person.tags == {tag_id('演员甲'), tag_id('別の名前')}

    def test_a_search_still_waiting_for_a_choice_does_not_count(self, cast):
        cast.movie('C-001', ('演员甲', '/star/a1'))
        db.StarLink.create(key='name:另一个写法', name='另一个写法', state=db.LINK_CHOOSE)
        assert actresses.find_people(['另一个写法']) == {'另一个写法': []}

    def test_the_search_name_leaves_out_the_number_that_tells_two_actresses_apart(self, cast):
        cast.actress('同名', 'jia', 'えんいんこう')
        cast.actress('同名（2）', 'yi', '同名', 'えんいんおつ')
        cast.actress('名字（2）', 'bing')
        cast.movie('C-001', ('演员丙', '/star/c1'))
        found = actresses.find_people(['同名（2）', 'えんいんこう', '名字（2）', '演员丙'])
        assert [(found[name][0].name, found[name][0].search_name) for name in found] == [
            ('同名（2）', '同名'), ('同名', '同名'), ('名字（2）', '名字（2）'), ('演员丙', '演员丙')]

    def test_the_search_key_of_each_kind_of_person(self, cast, make_item):
        jia = cast.actress('演员甲', 'jia')
        cast.movie('L-001')
        cast.legacy('L-001', 'yi', '演员乙')
        make_item('C-001', tags=[('star', '演员丁')])
        found = actresses.find_people(['演员甲', '演员乙', '演员丁'])
        assert [found[name][0].search_key for name in found] == [
            f'avwiki:{AV}jia/', f'avwiki:{AV}yi/', 'name:演员丁']
        assert jia.id


class TestPeopleOfTags:
    def test_each_tag_belongs_to_one_person(self, cast, make_item):
        jia = cast.actress('演员甲', 'jia', 'えんいんこう', javbus='/star/a1')
        cast.movie('C-001', ('別の名前', '/star/a1'), ('演员丙', '/star/c1'))
        cast.movie('L-001')
        cast.legacy('L-001', 'yi', '演员乙', 'えんいんおつ')
        make_item('C-002', tags=[('star', '演员丁')])
        ids = [tag_id(value) for value in ('演员甲', '別の名前', '演员丙', '演员乙', 'えんいんおつ', '演员丁')]
        people = actresses.people_of_tags(ids)
        assert [people[tid].key for tid in ids] == [
            f'actress:{jia.id}', f'actress:{jia.id}', 'javbus:/star/c1', f'avwiki:{AV}yi/', f'avwiki:{AV}yi/',
            f'tag:{tag_id("演员丁")}']
        assert people[ids[0]].name == '演员甲' and people[ids[3]].name == '演员乙'

    def test_tags_that_are_not_stars_have_no_person(self, make_item):
        make_item('C-001', tags=[('genre', '标签一')])
        assert actresses.people_of_tags([db.Tag.get(db.Tag.value == '标签一').id]) == {}


def web_write_before_the_first_transaction(monkeypatch):
    '''后台取好了要处理的名单、第一次开写事务（BEGIN IMMEDIATE）之前，网页线程上有一次写库（另一条数据库连接）'''
    real = db.db.atomic
    written = []

    def write():
        try:
            with db.db.atomic('IMMEDIATE'):
                db.JobState.create(name='web', text='网页上的一次写')
        finally:
            db.db.close()

    class BeforeTransaction:
        def __init__(self, inner):
            self.inner = inner

        def __enter__(self):
            if not written:
                written.append(True)
                thread = threading.Thread(target=write)
                thread.start()
                thread.join(timeout=10)
                assert not thread.is_alive()
            return self.inner.__enter__()

        def __exit__(self, *error):
            return self.inner.__exit__(*error)

    def atomic(*args, **kwargs):
        inner = real(*args, **kwargs)
        return BeforeTransaction(inner) if args == ('IMMEDIATE',) else inner

    monkeypatch.setattr(db.db, 'atomic', atomic)
    return written


class TestReconcile:
    '''知道 JavBus 演员页的女优，库里后来有了那个演员页的标签（别处抓来的影片）：正式名字换成它'''

    def test_her_name_becomes_the_one_javbus_gave_the_movie(self, cast):
        jia = cast.actress('演员甲', 'jia', '甲在 JavBus 上的名字', javbus='/star/a1')
        cast.movie('C-001', ('甲在 JavBus 上的名字', '/star/a1'))
        actresses.reconcile()
        assert db.Tag.get_by_id(db.Actress.get_by_id(jia.id).tag_id).value == '甲在 JavBus 上的名字'

    def test_a_write_from_the_web_before_the_first_of_them_does_not_stop_the_rest(self, cast, monkeypatch):
        jia = cast.actress('演员甲', 'jia', '甲在 JavBus 上的名字', javbus='/star/a1')
        yi = cast.actress('演员乙', 'yi', '乙在 JavBus 上的名字', javbus='/star/b2')
        cast.movie('C-001', ('甲在 JavBus 上的名字', '/star/a1'))
        cast.movie('C-002', ('乙在 JavBus 上的名字', '/star/b2'))
        written = web_write_before_the_first_transaction(monkeypatch)
        actresses.reconcile()
        assert written == [True]
        names = [db.Tag.get_by_id(db.Actress.get_by_id(person.id).tag_id).value for person in (jia, yi)]
        assert names == ['甲在 JavBus 上的名字', '乙在 JavBus 上的名字']


class TestAliasTable:
    def test_an_alias_keeps_its_folded_spelling(self, cast):
        jia = cast.actress('演员甲', 'jia', ' Actor A ', '學生')
        rows = db.ActressAlias.select().where(db.ActressAlias.actress == jia.id).order_by(db.ActressAlias.id)
        assert [(row.name, row.key) for row in rows] == [('Actor A', 'actor a'), ('學生', '学生')]

    def test_an_alias_is_stored_once_and_never_as_her_own_name(self, cast):
        jia = cast.actress('演员甲', 'jia', 'えんいんこう', 'えんいんこう', '演员甲', '')
        assert db.ActressAlias.select().where(db.ActressAlias.actress == jia.id).count() == 1


def her(actress):
    '''(正式名字, [别名…], JavBus 演员页)'''
    fresh = db.Actress.get_by_id(actress.id)
    aliases = [row.name for row in db.ActressAlias.select().where(db.ActressAlias.actress == fresh.id)
               .order_by(db.ActressAlias.id)]
    return db.Tag.get_by_id(fresh.tag_id).value, aliases, fresh.javbus


def filled_movie(cast, fanhao, actress, liked=False):
    '''一部演员是补名写的影片（有补名记录），挂着这位女优的正式名字；liked 时用户打了"喜欢"'''
    cast.movie(fanhao)
    cast.link(fanhao, actress)
    db.save_lookup(fanhao, db.LOOKUP_FILLED, 0, DAY, DAY)
    if liked:
        db.ItemRate.create(item=fanhao, rate_type=db.RATE_TYPE.USER_RATE, rate_value=db.RATE_VALUE.LIKE, rete_time=DAY)


def link(page, path, name):
    '''按名字查找到了她的 JavBus 演员页 path（演员页上的名字是 name），像后台查找那样在事务里记下'''
    with db.db.atomic('IMMEDIATE'):
        return actresses.found_javbus(f'avwiki:{page}', path, name)


def unlink(page, path):
    with db.db.atomic('IMMEDIATE'):
        return actresses.unlink_javbus(page, path)


BING = f'{AV}bing/'
STAR_Z = '/star/z1'


class TestHerOwnSpellingComesBack:
    '''
    关联的 JavBus 演员页上的名字和她的名字只差简繁、大小写（或者一字不差）：正式名字换成 JavBus 的那条以后，
    她自己这一侧的写法要留着，取消关联时换回它，不能换成她的读音
    '''

    @pytest.fixture
    def bing(self, cast):
        '''女优页上是「演员丙 / へい / Hei」，补过一部喜欢的影片；JavBus 演员页 z1 上写作「演員丙」，查找时自动采用了它'''
        bing, _ = actresses.read_page(BING, ['演员丙', 'へい', 'Hei'], DAY)
        filled_movie(cast, 'W-004', bing, liked=True)
        cast.movie('JB-008', ('演員丙', STAR_Z))
        assert link(BING, STAR_Z, '演員丙') == {'W-004'}
        return bing

    def test_her_own_spelling_is_kept_while_she_goes_by_the_javbus_one(self, bing, cast):
        assert her(bing) == ('演員丙', ['演员丙', 'へい', 'Hei'], STAR_Z)
        assert cast.stars_of('W-004') == ['演員丙'] and cast.stars_of('JB-008') == ['演員丙']

    def test_unlinking_gives_her_own_spelling_back(self, bing, cast):
        assert unlink(BING, STAR_Z) == {'W-004'}
        assert her(bing) == ('演员丙', ['へい', 'Hei'], '')
        assert cast.stars_of('W-004') == ['演员丙'] and cast.stars_of('JB-008') == ['演員丙']
        assert db.Tag.get(db.Tag.value == '演員丙').url == STAR_Z
        assert db.Tag.get(db.Tag.value == '演员丙').url == BING

    def test_her_page_gone_after_unlinking_leaves_her_own_spelling(self, bing):
        unlink(BING, STAR_Z)
        assert [actress.id for actress in actresses.pages_to_read()] == [bing.id]
        actresses.page_missing(BING, DAY)
        assert her(bing)[0] == '演员丙'
        assert actresses.pages_to_read() == []

    def test_her_own_spelling_stays_when_her_page_is_read_again_while_linked(self, bing):
        actresses.read_page(BING, ['演员丙', 'へい', 'Hei'], DAY)
        assert her(bing) == ('演員丙', ['演员丙', 'へい', 'Hei'], STAR_Z)
        unlink(BING, STAR_Z)
        assert her(bing)[:2] == ('演员丙', ['へい', 'Hei'])

    def test_searching_again_after_unlinking_uses_her_own_spelling(self, bing):
        unlink(BING, STAR_Z)
        assert actresses.search_name_of(BING) == '演员丙'

    def test_unlinking_a_link_an_earlier_version_made_uses_the_name_she_was_searched_with(self, bing, cast):
        # 以前的版本换成 JavBus 的写法时没留下她自己的写法；查找记录里记着当时查找用的名字
        db.ActressAlias.delete().where((db.ActressAlias.actress == bing.id) & (db.ActressAlias.name == '演员丙')).execute()
        db.StarLink.create(key=f'avwiki:{BING}', name='演员丙', state=db.LINK_FOUND, path=STAR_Z,
                           candidates=f'[["{STAR_Z}", "演員丙"]]')
        unlink(BING, STAR_Z)
        assert her(bing)[:2] == ('演员丙', ['へい', 'Hei'])
        assert cast.stars_of('W-004') == ['演员丙']

    def test_a_reading_left_by_an_earlier_version_goes_away_when_her_page_is_read(self, cast):
        # 以前的版本取消关联时把她改成了读音（第二个名字）；读得到女优页时按页面上的先后改回来
        bing, _ = actresses.read_page(BING, ['演员丙', 'へい', 'Hei'], DAY)
        filled_movie(cast, 'W-004', bing, liked=True)
        cast.movie('JB-008', ('演員丙', STAR_Z))
        actresses._set_rejected(BING, {STAR_Z})
        reading = db.Tag.create(type_='star', value='へい', url=BING)
        actresses._switch(db.Actress.get_by_id(bing.id), reading)
        assert her(bing)[0] == 'へい' and cast.stars_of('W-004') == ['へい']
        actresses.read_page(BING, ['演员丙', 'へい', 'Hei'], DAY)
        assert her(bing)[:2] == ('演员丙', ['へい', 'Hei'])
        assert cast.stars_of('W-004') == ['演员丙'] and cast.stars_of('JB-008') == ['演員丙']

    def test_the_same_name_as_the_javbus_one_becomes_her_numbered_name(self, cast):
        cast.movie('JB-001', ('演员甲', '/star/a1'))
        jia, _ = actresses.read_page(f'{AV}jia/', ['演员甲', 'えんいんこう'], DAY)
        filled_movie(cast, 'W-001', jia)
        assert her(jia) == ('演员甲', ['演员甲', 'えんいんこう'], '/star/a1')
        unlink(f'{AV}jia/', '/star/a1')
        # 两个人同名：JavBus 的那位留着「演员甲」，她用「演员甲（2）」，和取消关联以后再读她的女优页一样
        assert her(jia) == ('演员甲（2）', ['演员甲', 'えんいんこう'], '')
        assert cast.stars_of('W-001') == ['演员甲（2）'] and cast.stars_of('JB-001') == ['演员甲']
        actresses.read_page(f'{AV}jia/', ['演员甲', 'えんいんこう'], DAY)
        assert her(jia) == ('演员甲（2）', ['演员甲', 'えんいんこう'], '')

    def test_a_numbered_name_linked_to_the_page_of_her_name_comes_back_numbered(self, cast):
        jia, _ = actresses.read_page(f'{AV}jia/', ['演员甲', 'えんいんこう'], DAY)
        yi, _ = actresses.read_page(f'{AV}yi/', ['演员甲', 'えんいんおつ'], DAY)
        assert her(yi)[0] == '演员甲（2）'
        filled_movie(cast, 'W-002', yi)
        cast.movie('JB-009', ('演員甲', '/star/a9'))
        link(f'{AV}yi/', '/star/a9', '演員甲')
        # 关联期间她的号留在别名的最后：设定里存的「演员甲（2）」照样认得她，别人也不会再编到这个号
        assert her(yi) == ('演員甲', ['演员甲', 'えんいんおつ', '演员甲（2）'], '/star/a9')
        unlink(f'{AV}yi/', '/star/a9')
        assert her(yi) == ('演员甲（2）', ['演员甲', 'えんいんおつ'], '')
        assert cast.stars_of('W-002') == ['演员甲（2）'] and her(jia)[0] == '演员甲'

    def test_a_javbus_spelling_she_was_unlinked_from_is_not_kept_as_her_name(self, bing):
        unlink(BING, STAR_Z)
        found = {person.key: person for person in actresses.find_people(['演员丙'])['演员丙']}
        person = found[f'actress:{bing.id}']
        assert (person.name, person.aliases, person.javbus) == ('演员丙', ('へい', 'Hei'), '')
        assert found[f'javbus:{STAR_Z}'].name == '演員丙'


SHIN = f'{AV}shin/'
STAR_S = '/star/s1'


def as_an_earlier_version_left_it(actress, spelling):
    '''以前的版本正式名字用 JavBus 演员页上的标签时，别名里不记和它折过以后相同的名字（她自己这一侧的写法）'''
    mine = (db.ActressAlias.actress == actress.id) & (db.ActressAlias.name == spelling)
    db.ActressAlias.delete().where(mine).execute()


class TestUnlinkingWhatAnEarlierFillLinked:
    '''
    以前的版本补名时，她的名字和 JavBus 带来的演员标签一字不差，就直接用了那条标签：没有查找记录，别名里也没有她自己的
    这个写法。取消关联以后马上就是她自己的名字（和那位 JavBus 演员分开，写成"名字（2）"），不靠读女优页
    '''

    @pytest.fixture
    def shin(self, cast):
        '''女优页上是「演员辛 / しん / Shin」，JavBus 演员页 s1 上的「演员辛」和她一字不差；补过一部喜欢的影片'''
        cast.movie('JB-009', ('演员辛', STAR_S))
        shin, _ = actresses.read_page(SHIN, ['演员辛', 'しん', 'Shin'], DAY)
        filled_movie(cast, 'W-009', shin, liked=True)
        as_an_earlier_version_left_it(shin, '演员辛')
        assert her(shin) == ('演员辛', ['しん', 'Shin'], STAR_S)
        assert not db.StarLink.select().where(db.StarLink.key == f'avwiki:{SHIN}').exists()
        return shin

    def test_unlinking_gives_her_numbered_name_at_once(self, shin, cast):
        assert unlink(SHIN, STAR_S) == {'W-009'}
        assert (her(shin)[0], her(shin)[2]) == ('演员辛（2）', '')
        assert cast.stars_of('W-009') == ['演员辛（2）'] and cast.stars_of('JB-009') == ['演员辛']
        assert db.Tag.get(db.Tag.value == '演员辛').url == STAR_S

    def test_her_page_gone_after_unlinking_leaves_her_numbered_name(self, shin):
        unlink(SHIN, STAR_S)
        assert [actress.id for actress in actresses.pages_to_read()] == [shin.id]
        actresses.page_missing(SHIN, DAY)
        assert her(shin)[0] == '演员辛（2）'
        assert actresses.pages_to_read() == []

    def test_reading_her_page_afterwards_does_not_change_her_name_again(self, shin, cast):
        unlink(SHIN, STAR_S)
        _, changed = actresses.read_page(SHIN, ['演员辛', 'しん', 'Shin'], DAY)
        assert changed == set()
        assert her(shin) == ('演员辛（2）', ['演员辛', 'しん', 'Shin'], '')
        assert cast.stars_of('W-009') == ['演员辛（2）']

    def test_searching_again_uses_her_name_without_the_number(self, shin):
        unlink(SHIN, STAR_S)
        assert actresses.search_name_of(SHIN) == '演员辛'

    def test_a_write_the_busy_database_cuts_short_leaves_her_as_she_was(self, shin, cast, monkeypatch):
        # 换名字、新编的号和别名是同一个事务：写到别名那一步库被占着，名字、号、影片上的挂接都回到原样
        def busy(*args, **kwargs):
            raise OperationalError('database is locked')

        before = (her(shin), cast.stars_of('W-009'))
        monkeypatch.setattr(actresses, '_put_aliases', busy)
        with pytest.raises(OperationalError):
            unlink(SHIN, STAR_S)
        assert (her(shin), cast.stars_of('W-009')) == before
        assert db.Tag.get_or_none(db.Tag.value == '演员辛（2）') is None
        assert not db.StarLink.select().where(db.StarLink.key == f'avwiki:{SHIN}').exists()

    def test_her_page_decides_when_that_name_was_not_her_first_one(self, cast):
        # 一字不差的那个名字不一定排在她女优页的第一个：库里分不出来，先按它定；读到女优页时按页面上的先后改
        cast.movie('JB-010', ('乙名', '/star/s2'))
        ren, _ = actresses.read_page(f'{AV}ren/', ['甲名', '乙名', 'じん'], DAY)
        filled_movie(cast, 'W-010', ren)
        as_an_earlier_version_left_it(ren, '乙名')
        assert her(ren) == ('乙名', ['甲名', 'じん'], '/star/s2')
        unlink(f'{AV}ren/', '/star/s2')
        assert her(ren)[0] == '乙名（2）'
        actresses.read_page(f'{AV}ren/', ['甲名', '乙名', 'じん'], DAY)
        assert her(ren)[0] == '甲名'
        assert cast.stars_of('W-010') == ['甲名'] and cast.stars_of('JB-010') == ['乙名']

    def test_a_link_made_through_a_search_still_goes_by_her_other_names(self, cast):
        # 有查找记录（查找来的关联）、记录里的名字又不是这条标签的写法：照原来的办法，按别名的先后定
        shin, _ = actresses.read_page(SHIN, ['演员辛', 'しん', 'Shin'], DAY)
        filled_movie(cast, 'W-011', shin)
        cast.movie('JB-011', ('Shin', '/star/s3'))
        db.StarLink.create(key=f'avwiki:{SHIN}', name='演员辛', state=db.LINK_FOUND, path='/star/s3',
                           candidates='[["/star/s3", "Shin"]]')
        link(SHIN, '/star/s3', 'Shin')
        as_an_earlier_version_left_it(shin, 'Shin')
        assert her(shin) == ('Shin', ['演员辛', 'しん'], '/star/s3')
        unlink(SHIN, '/star/s3')
        assert her(shin)[0] == '演员辛'
        assert cast.stars_of('W-011') == ['演员辛']


YI = f'{AV}yi/'
STAR_A = '/star/a9'


class TestHerNumberStaysHers:
    '''
    为了和同名的人分开给她编的"名字（N）"一直是她的：别人不会再编到这个号，设定里存的这个名字一直认得她
    （她改用 JavBus 演员页上的名字期间也是），取消关联以后她回到同一个号；这个号不当她的名字列出来
    '''

    @pytest.fixture
    def yi(self, cast):
        '''「演员甲」有两位：第二位是「演员甲（2）」，补过一部影片；查找到的 JavBus 演员页 a9 上写作「演員甲」'''
        actresses.read_page(f'{AV}jia/', ['演员甲', 'えんいんこう'], DAY)
        yi, _ = actresses.read_page(YI, ['演员甲', 'えんいんおつ'], DAY)
        filled_movie(cast, 'W-002', yi)
        cast.movie('JB-009', ('演員甲', STAR_A))
        assert her(yi)[0] == '演员甲（2）'
        return yi

    def test_a_number_kept_as_an_alias_is_not_given_to_anyone_else(self, cast):
        # 以前的版本把第二位编成"同名（3）"、别名里留着"同名（2）"，设定里可能存着它
        cast.actress('同名', 'jia', 'どうめい')
        second = cast.actress('同名（3）', 'second', '同名', 'どうめいに', '同名（2）')
        third, _ = actresses.read_page(f'{AV}third/', ['同名', 'どうめいさん'], DAY)
        assert her(third)[0] == '同名（4）'
        person, = actresses.find_people(['同名（2）'])['同名（2）']
        assert person.actress == second.id

    def test_another_spelling_of_a_number_in_use_is_skipped(self, cast):
        cast.actress('演员甲', 'jia')
        cast.actress('演員甲（2）', 'second', '演員甲')
        third, _ = actresses.read_page(f'{AV}third/', ['演员甲', 'えんいんへい'], DAY)
        assert her(third)[0] == '演员甲（3）'

    def test_her_number_written_in_the_other_spelling_stays_when_her_page_is_read(self, cast):
        cast.actress('演员甲', 'jia')
        second = cast.actress('演員甲（2）', 'second', '演員甲')
        _, changed = actresses.read_page(f'{AV}second/', ['演员甲', 'えんいんおつ'], DAY)
        assert changed == set() and her(second)[0] == '演員甲（2）'

    def test_her_number_finds_her_while_she_goes_by_the_javbus_name(self, yi):
        link(YI, STAR_A, '演員甲')
        assert her(yi)[0] == '演員甲'
        person, = actresses.find_people(['演员甲（2）'])['演员甲（2）']
        assert (person.actress, person.name, person.javbus, person.avwiki) == (yi.id, '演員甲', STAR_A, YI)
        assert person.aliases == ('えんいんおつ',)

    def test_her_number_is_not_given_to_a_newcomer_while_she_is_linked(self, yi):
        link(YI, STAR_A, '演員甲')
        third, _ = actresses.read_page(f'{AV}third/', ['演员甲', 'えんいんへい'], DAY)
        assert her(third)[0] == '演员甲（3）'
        assert [person.actress for person in actresses.find_people(['演员甲（2）'])['演员甲（2）']] == [yi.id]

    def test_unlinking_gives_her_the_same_number_back(self, yi, cast):
        link(YI, STAR_A, '演員甲')
        actresses.read_page(f'{AV}third/', ['演员甲', 'えんいんへい'], DAY)
        unlink(YI, STAR_A)
        assert her(yi) == ('演员甲（2）', ['演员甲', 'えんいんおつ'], '')
        assert cast.stars_of('W-002') == ['演员甲（2）'] and cast.stars_of('JB-009') == ['演員甲']
        assert her(db.Actress.get(db.Actress.avwiki == f'{AV}third/'))[0] == '演员甲（3）'

    def test_her_number_stays_when_her_page_is_read_while_she_is_linked(self, yi):
        link(YI, STAR_A, '演員甲')
        actresses.read_page(YI, ['演员甲', 'えんいんおつ'], DAY)
        assert her(yi)[0] == '演員甲'
        assert [person.actress for person in actresses.find_people(['演员甲（2）'])['演员甲（2）']] == [yi.id]

    def test_her_number_is_never_the_name_searched_with(self, yi):
        assert actresses.find_people(['演员甲（2）'])['演员甲（2）'][0].search_name == '演员甲'
        link(YI, STAR_A, '演員甲')
        assert actresses.find_people(['演员甲（2）'])['演员甲（2）'][0].search_name == '演員甲'
        unlink(YI, STAR_A)
        assert actresses.search_name_of(YI) == '演员甲'

    def test_a_number_an_earlier_version_kept_as_an_alias_is_not_listed_as_her_name(self, cast):
        cast.actress('同名', 'jia')
        cast.actress('同名（3）', 'second', '同名', 'どうめいに', '同名（2）')
        person, = actresses.find_people(['同名（2）'])['同名（2）']
        assert (person.name, person.aliases, person.search_name) == ('同名（3）', ('同名', 'どうめいに'), '同名')
