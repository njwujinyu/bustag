'''
日志和命令行输出里写什么：只写番号、数量这些定位问题用得上的东西，不写影片标题、标签的名字、演员的名字和
本地文件的地址（文件名里常带标题）；打标要在 INFO 级别留下记录
'''
import logging

import pytest

from bustag.app import local
from bustag.spider import bus_spider, db
from bustag.spider.bus_spider import DownloadResult
from bustag.spider.db import RATE_TYPE, RATE_VALUE, ItemRate, LocalItem
from bustag.spider.parser import Tag

TITLE = '编造的标题不该进日志'
GENRE = '编造的类别'
STAR = '编造的演员'
FILE = 'http://nas.local/编造的标题不该进日志.mp4'


def movie(fanhao='ABC-001'):
    meta = {'fanhao': fanhao, 'title': TITLE, 'url': '/' + fanhao, 'release_date': '2026-01-01',
            'cover_img_url': '/pics/cover/x_b.jpg', 'length': '120'}
    return meta, [Tag('genre', GENRE, '/genre/1'), Tag('star', STAR, '/star/1')]


@pytest.fixture
def lines(caplog):
    '''调用后返回到现在为止写进日志的每一行（DEBUG 以上都算），连同异常栈的文字'''
    caplog.set_level(logging.DEBUG, logger='bustag')

    def written(level=logging.DEBUG):
        return [record.getMessage() for record in caplog.records if record.levelno >= level]
    written.clear = caplog.clear
    written.everything = lambda: caplog.text
    return written


def tag_id(value):
    return db.Tag.get(db.Tag.value == value).id


class TestSavingAMovie:
    def test_a_new_movie_is_written_by_its_code_and_its_tags_by_their_ids(self, lines):
        db.save(*movie())
        genre, star = tag_id(GENRE), tag_id(STAR)
        assert lines() == ['已保存影片 ABC-001',
                           f'已保存标签 #{genre}（genre）', f'影片 ABC-001 加上标签 #{genre}',
                           f'已保存标签 #{star}（star）', f'影片 ABC-001 加上标签 #{star}']

    def test_a_movie_that_is_already_there(self, lines):
        db.save(*movie())
        lines.clear()
        assert db.save(*movie()) is None
        assert lines() == ['影片已在库里：ABC-001']

    def test_a_tag_that_cannot_be_attached(self, lines):
        item = db.save(*movie())
        tag = db.Tag.get(db.Tag.value == GENRE)
        lines.clear()
        assert db.ItemTag.saveit(item, tag) is None
        assert lines() == [f'影片 ABC-001 加标签 #{tag.id} 失败']
        assert GENRE not in lines.everything() and TITLE not in lines.everything()

    def test_what_the_rows_look_like_when_printed(self):
        item = db.save(*movie())
        tag = db.Tag.get(db.Tag.value == GENRE)
        link = db.ItemTag.get((db.ItemTag.item == 'ABC-001') & (db.ItemTag.tag == tag.id))
        local_item = LocalItem.saveit('ABC-001', FILE)
        assert repr(item) == '<Item ABC-001>'
        assert repr(tag) == f'<Tag {tag.id} genre>'
        assert repr(link) == f'<ItemTag ABC-001 - {tag.id}>'
        assert repr(local_item) == '<LocalItem ABC-001>'


class TestDownload:
    @pytest.fixture
    def parsed(self, monkeypatch):
        '''详情页解析出来的就是那部编造的影片'''
        monkeypatch.setattr(bus_spider, 'parse_item', lambda html: movie())

    def test_a_downloaded_movie_is_written_by_its_code(self, lines, fake_fetcher, parsed):
        result = DownloadResult()
        assert bus_spider._download_item(fake_fetcher({'/ABC-001': '<html></html>'}), result, '/ABC-001', 'ABC-001')
        assert result.added == 1
        assert lines(logging.INFO) == ['新增 ABC-001']
        assert not any(word in lines.everything() for word in (TITLE, GENRE, STAR))

    def test_a_movie_downloaded_again_is_written_by_its_code(self, lines, fake_fetcher, parsed):
        db.save(*movie())
        lines.clear()
        result = DownloadResult()
        assert bus_spider._download_item(fake_fetcher({'/ABC-001': '<html></html>'}), result, '/ABC-001', 'ABC-001')
        assert result.skipped == 1
        assert lines() == ['影片已在库里：ABC-001', 'ABC-001 已在库里，跳过']


class TestLocalFiles:
    def test_playing_a_file_is_written_by_its_number_and_code(self, wsgi, lines):
        db.save(*movie())
        local_item = LocalItem.saveit('ABC-001', FILE)
        lines.clear()
        assert wsgi.get(f'/local_play/{local_item.id}').status == 303
        assert lines() == [f'本地文件 {local_item.id} 观看次数加 1（更新了 1 行）', f'播放本地文件 {local_item.id}（ABC-001）']
        assert '编造' not in lines.everything()

    def test_uploading_codes_tells_how_many_files_and_tags_were_added(self, lines):
        db.save(*movie())
        lines.clear()
        local.add_local_fanhao(f'ABC-001,{FILE}\nABC-002\nabc003,http://nas.local/3.mp4', tag_like=True)
        assert lines(logging.INFO) == ['上传番号：新增本地文件 2 个，打标为喜欢 3 条，库里还没有的番号 2 个']
        assert '编造' not in lines.everything()

    def test_uploading_codes_without_tagging(self, lines):
        local.add_local_fanhao('ABC-001', tag_like=False)
        assert lines(logging.INFO) == ['上传番号：新增本地文件 0 个，打标为喜欢 0 条，库里还没有的番号 1 个']


class TestTagging:
    '''打标成功时写一条 INFO：番号、喜欢还是不喜欢、是新增还是改动'''

    def post(self, wsgi, lines, fanhao, submit):
        lines.clear()
        assert wsgi.post(f'/tag/{fanhao}', {'submit': submit}).status == 303
        return lines(logging.INFO)

    def test_a_movie_without_any_rate(self, wsgi, lines, fill_requests):
        db.save(*movie())
        assert self.post(wsgi, lines, 'ABC-001', '1') == ['打标 ABC-001：喜欢（新增）']

    def test_a_movie_the_model_had_rated(self, wsgi, lines):
        db.save(*movie())
        ItemRate.saveit(RATE_TYPE.SYSTEM_RATE, RATE_VALUE.LIKE, 'ABC-001')
        assert self.post(wsgi, lines, 'ABC-001', '0') == ['打标 ABC-001：不喜欢（新增，原来是模型给的评分）']

    def test_a_movie_the_user_had_tagged(self, wsgi, lines, fill_requests):
        db.save(*movie())
        ItemRate.saveit(RATE_TYPE.USER_RATE, RATE_VALUE.DISLIKE, 'ABC-001')
        assert self.post(wsgi, lines, 'ABC-001', '1') == ['打标 ABC-001：喜欢（改动，原来已经打过标）']
        assert self.post(wsgi, lines, 'ABC-001', '0') == ['打标 ABC-001：不喜欢（改动，原来已经打过标）']

    def test_nothing_is_written_when_nothing_was_tagged(self, wsgi, lines):
        lines.clear()
        assert wsgi.post('/tag/ABC-404', {'submit': '1'}).status == 404
        db.save(*movie())
        assert wsgi.post('/tag/ABC-001', {'submit': '2'}).status == 400
        assert [line for line in lines(logging.INFO) if line.startswith('打标')] == []

    def test_no_title_or_name_goes_with_it(self, wsgi, lines, fill_requests):
        db.save(*movie())
        self.post(wsgi, lines, 'ABC-001', '1')
        assert not any(word in lines.everything() for word in (TITLE, GENRE, STAR))
