'''
比较文字时用的写法（db.fold）：去掉首尾空白、英文字母不分大小写、繁体字折成简体字。
库里的值和用户输入的文字用同一个函数折，所以简体和繁体互通；日文写法不转换
'''
import pytest

from bustag import hanzi
from bustag.spider import db


class TestTable:
    def test_every_traditional_character_has_one_simplified_counterpart(self):
        assert len(hanzi.TRADITIONAL) == len(hanzi.SIMPLIFIED) > 2400
        assert len(set(hanzi.TRADITIONAL)) == len(hanzi.TRADITIONAL)
        assert all(traditional != simplified for traditional, simplified in zip(hanzi.TRADITIONAL, hanzi.SIMPLIFIED))

    def test_folding_twice_changes_nothing_more(self):
        # 没有哪个字既是"要换掉的"又是"换成的"
        assert set(hanzi.TRADITIONAL).isdisjoint(hanzi.SIMPLIFIED)

    def test_only_single_chinese_characters_of_the_basic_plane(self):
        for char in hanzi.TRADITIONAL + hanzi.SIMPLIFIED:
            assert 0x3400 <= ord(char) <= 0x9FFF or 0xF900 <= ord(char) <= 0xFAFF, hex(ord(char))

    def test_the_file_says_how_it_is_made(self):
        assert 'tools/make_hanzi_table.py' in hanzi.__doc__ and 'LCMapStringEx' in hanzi.__doc__
        assert f'共 {len(hanzi.TRADITIONAL)} 个' in hanzi.__doc__


class TestFold:
    @pytest.mark.parametrize('text, folded', [
        ('學生', '学生'),
        ('学生', '学生'),
        ('斡瀠鰉', '斡潆鳇'),
        ('女大學生', '女大学生'),
        ('膿燈作瓰', '脓灯作瓰'),
        ('製作商', '制作商'),
        ('發行商', '发行商'),
        ('導演', '导演'),
        ('頭髮', '头发'),
        ('氣質', '气质'),
    ], ids=range(10))
    def test_traditional_and_simplified_spellings_fold_to_the_same_text(self, text, folded):
        assert db.fold(text) == folded
        assert db.fold(folded) == folded

    @pytest.mark.parametrize('text', ['気', '図', '駅', '学', 'ひらがな', 'カタカナ', '4K', '演员甲', 'デカ拳', '😀', ''])
    def test_japanese_spellings_and_everything_else_stay_as_they_are(self, text):
        assert db.fold(text) == text.lower()

    def test_a_character_japanese_shares_with_traditional_chinese_folds_like_the_chinese_one(self):
        # 学園 的 園 就是繁体的 園：库里的值和输入的文字都这样折，两边仍然对得上
        assert db.fold('学園') == db.fold('學園') == db.fold('学园') == '学园'

    def test_a_japanese_simplified_form_is_not_taken_for_the_traditional_one(self):
        # 氣（繁体）折成 气；気（日文新字体）不在表里，和它们不算同一个字
        assert db.fold('氣') == db.fold('气') == '气'
        assert db.fold('気') == '気'

    def test_english_letters_ignore_case_and_only_they_do(self):
        assert db.fold('Studio ONE') == db.fold('studio one') == 'studio one'
        # 和 SQLite 的 NOCASE 一样只管 ASCII 字母
        assert db.fold('ÀÉ Ω') == 'ÀÉ Ω'

    @pytest.mark.parametrize('blank', [' ', '\u3000', '\t', '\xa0', '\n', ' \u3000\t\xa0\r\n'],
                             ids=['space', 'full-width', 'tab', 'no-break', 'newline', 'mixed'])
    def test_blanks_of_any_kind_around_the_text_are_dropped(self, blank):
        assert db.fold(f'{blank}學生 甲{blank}') == '学生 甲'

    def test_the_database_folds_with_the_same_function(self):
        row = db.db.execute_sql('SELECT bustag_fold(?), bustag_fold(?), bustag_fold(NULL)',
                                ('\u3000Studio 學生\t', '気')).fetchone()
        assert row == ('studio 学生', '気', None)

    def test_the_function_is_there_on_every_new_connection(self):
        db.db.close()
        db.db.connect()
        assert db.db.execute_sql("SELECT bustag_fold('畫')").fetchone() == ('画',)
