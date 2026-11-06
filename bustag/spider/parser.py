'''
解析 JavBus 页面（BeautifulSoup + 内置 html.parser）
'''
import re
from collections import namedtuple
from datetime import date
from urllib.parse import urlparse

from bs4 import BeautifulSoup

Tag = namedtuple('Tag', ['type', 'value', 'link'])

# 同番号的第二部影片：详情页路径在番号后面加发行日期（/GEBB-062_2026-10-31），页面上的識別碼和第一部相同
DATED_SUFFIX = re.compile(r'_\d{4}-\d{2}-\d{2}$')

# 详情页里"标题 + 一个链接"的字段，值是老库里的标签类型（带冒号）
HEADER_TAG_TYPES = {'導演': '導演:', '製作商': '製作商:', '發行商': '發行商:', '系列': '系列:'}
# 类别和演员按链接路径区分
LINK_TAG_TYPES = (('/genre/', 'genre'), ('/star/', 'star'))


class ParseError(Exception):
    '''
    页面不是影片详情页（验证页、错误页或页面结构变了）
    '''


def _soup(html):
    return BeautifulSoup(html, 'html.parser')


def _path(href):
    return urlparse(href).path


def _field_value(p, header):
    texts = (s.strip() for s in p.find_all(string=True)
             if all(parent is not header for parent in s.parents))
    return ' '.join(t for t in texts if t)


def _parse_date(text):
    match = re.search(r'\d{4}-\d{2}-\d{2}', text or '')
    if not match:
        return None
    try:
        return date.fromisoformat(match.group()).isoformat()
    except ValueError:
        return None


def _unique(tags):
    seen = set()
    result = []
    for tag in tags:
        key = (tag.type, tag.value)
        if key not in seen:
            seen.add(key)
            result.append(tag)
    return result


def parse_item(html):
    '''
    Args:
        html : str - 详情页 html

    Returns:
        tuple: (dict, list)
        dict - fanhao, title, cover_img_url（页面原值）, release_date（ISO 字符串或 None）,
               length（数字字符串或 None）
        list - Tag 列表，link 只有路径
    '''
    soup = _soup(html)
    heading = soup.select_one('div.container > h3')
    info = soup.select_one('div.info')
    if heading is None or info is None:
        raise ParseError('不是影片详情页：找不到标题或影片信息（可能是验证页，或页面结构变了）')

    fields = {}
    tags = []
    for p in info.find_all('p'):
        header = p.find('span', class_='header')
        if header is None:
            continue
        name = header.get_text(strip=True).rstrip(':：').strip()
        if name in HEADER_TAG_TYPES:
            link = p.find('a', href=True)
            if link is not None and link.get_text(strip=True):
                tags.append(Tag(HEADER_TAG_TYPES[name], link.get_text(strip=True),
                                _path(link['href'])))
        else:
            fields[name] = _field_value(p, header)
    for link in info.select('p span.genre a[href]'):
        path = _path(link['href'])
        value = link.get_text(strip=True)
        for marker, tag_type in LINK_TAG_TYPES:
            if marker in path and value:
                tags.append(Tag(tag_type, value, path))
                break

    title = heading.get_text(' ', strip=True)
    fanhao = fields.get('識別碼') or (title.split(maxsplit=1)[0] if title else '')
    if not fanhao:
        raise ParseError('不是影片详情页：找不到番号')
    if title.startswith(fanhao):
        title = title[len(fanhao):].strip()

    cover_img_url = ''
    cover = soup.select_one('div.screencap a.bigImage')
    if cover is not None:
        cover_img_url = cover.get('href') or ''
        if not cover_img_url and cover.img is not None:
            cover_img_url = cover.img.get('src') or ''

    length = re.search(r'\d+', fields.get('長度', ''))
    meta = {
        'fanhao': fanhao,
        'title': title,
        'cover_img_url': cover_img_url,
        'release_date': _parse_date(fields.get('發行日期')),
        'length': length.group() if length else None,
    }
    return meta, _unique(tags)


def parse_list(html):
    '''
    列表页里影片详情页的路径（如 /IENF-465），去重并保持顺序
    '''
    soup = _soup(html)
    paths = []
    for link in soup.select('a.movie-box[href]'):
        path = _path(link['href'])
        if re.fullmatch(r'/[^/]+', path) and path not in paths:
            paths.append(path)
    return paths


def has_next_page(html):
    soup = _soup(html)
    return soup.select_one('ul.pagination a#next') is not None


def fanhao_from_path(path):
    '''
    详情页路径对应的入库番号，就是路径里的编号（/IENF-465 → IENF-465）。
    同番号的第二部影片路径带日期后缀（/GEBB-062_2026-10-31），后缀保留，两部分开入库
    '''
    return path.strip('/')


def is_valid_fanhao(text):
    '''
    番号只能由字母、数字、横线、下划线组成，以字母或数字开头，最长 64 个字符：
    番号会拼进详情页网址和封面缓存的文件名，导入的、用户提交的番号都要先过这一关
    '''
    return isinstance(text, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', text) is not None
