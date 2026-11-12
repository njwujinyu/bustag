'''
解析 JavBus 页面（BeautifulSoup + 内置 html.parser）
'''
import re
from collections import namedtuple
from datetime import date
from urllib.parse import quote, urljoin, urlparse, urlunsplit

from bs4 import BeautifulSoup, NavigableString

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


# JavBus 演员页的路径：有码区 /star/<编号>，无码区 /uncensored/star/<编号>。编号是字母和数字
STAR_PAGE = re.compile(r'/(?:uncensored/)?star/[0-9A-Za-z_-]{1,40}')
# 列表页上方写着的两个数，标签后面可以有冒号、括号、空白，数字可以带千位分隔的逗号
_LIST_COUNT = r'{label}\s*[:：]?\s*[(（]?\s*([0-9][0-9,]*)'


def is_star_page(path):
    '''这个路径是不是 JavBus 的演员页（第 1 页）'''
    return isinstance(path, str) and STAR_PAGE.fullmatch(path) is not None


def list_page_path(path, page):
    '''列表的第 page 页：第 1 页就是 path，之后是 path/<页>'''
    return path if page == 1 else f'{path}/{page}'


def parse_list_counts(html):
    '''
    演员页（类别页也一样）上方写的两个数：(已有磁力的部数, 全部影片的部数)，读不到的是 None
    '''
    text = _soup(html).get_text(' ', strip=True)

    def number(label):
        found = re.search(_LIST_COUNT.format(label=label), text)
        return int(found.group(1).replace(',', '')) if found else None

    return number('已有磁力'), number('全部影片')


def searchstar_path(name):
    '''JavBus 按名字找演员的路径：名字整个转义（里面的斜杠、问号也是名字的一部分）'''
    return '/searchstar/' + quote(name, safe='')


def _own_text(node):
    '''
    标签自己的直接文字，首尾空白去掉。嵌套的标签和注释里的字不算：
    演员框里名字后面嵌着一个写着角标的按钮，角标不是名字的一部分
    '''
    return ''.join(child for child in node.children if type(child) is NavigableString).strip()


def parse_star_search(html):
    '''
    按名字找演员的结果：[(演员页路径, 名字)]，按页面上的先后、去重。结果是一个个演员框（a.avatar-box），
    名字是框里 photo-info 的 span 自己的直接文字，没有时取头像的 title；
    页面换了写法、没有这个类时，退一步认列表里链到演员页的链接
    '''
    soup = _soup(html)
    links = soup.select('a.avatar-box[href]') or soup.select('#waterfall a[href]')
    found, seen = [], set()
    for link in links:
        path = _path(link['href'])
        if not STAR_PAGE.fullmatch(path) or path in seen:
            continue
        label = link.select_one('.photo-info span') or link.select_one('span')
        image = link.select_one('img[title]')
        name = _own_text(label) if label is not None else ''
        if not name and image is not None:
            name = image['title'].strip()
        if name:
            seen.add(path)
            found.append((path, name))
    return found


def parse_list_entries(html):
    '''
    列表页里每部影片的 (详情路径, 发行日期)，从上到下（演员页上就是从新到旧），去重。
    发行日期是影片框里写成日期的那个 <date>，没有时为 None
    '''
    entries = []
    seen = set()
    for link in _soup(html).select('a.movie-box[href]'):
        path = _path(link['href'])
        if not re.fullmatch(r'/[^/]+', path) or path in seen:
            continue
        seen.add(path)
        dates = [node.get_text(strip=True) for node in link.find_all('date')]
        release = next((text for text in dates if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text)), None)
        entries.append((path, release))
    return entries


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


def same_code(fanhao):
    '''
    去掉日期后缀后的番号（AEGE-032_2024-06-20 → AEGE-032），用来判断是不是同一識別碼
    '''
    if not isinstance(fanhao, str):
        return ''
    return DATED_SUFFIX.sub('', fanhao.strip()).upper()


def detail_path_for(text, fanhao):
    '''
    用户贴的详情地址收成路径。必须和 fanhao 是同一識別碼（可以带日期后缀），否则返回 None
    '''
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip()
    path = urlparse(raw).path if '://' in raw else raw
    path = '/' + path.strip('/').upper()
    if not re.fullmatch(r'/[A-Za-z0-9][A-Za-z0-9_-]{0,63}', path):
        return None
    if same_code(path.strip('/')) != same_code(fanhao):
        return None
    return path


def parse_code_entries(html, code):
    '''
    搜索页或列表页里，識別碼等于 code 的详情路径和发行日期。
    影片框里的两条 date：前面是識別碼，后面是发行日期。
    只认識別碼完全相同的路径；多一个零的编号不算同一番号。顺序保持页面上的顺序
    '''
    wanted = same_code(code)
    if not wanted:
        return []
    soup = _soup(html)
    entries = []
    seen = set()
    for link in soup.select('a.movie-box[href]'):
        path = _path(link['href']).upper()
        if not re.fullmatch(r'/[^/]+', path) or same_code(path.strip('/')) != wanted:
            continue
        dates = [node.get_text(strip=True) for node in link.find_all('date')]
        release = next((text for text in dates if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text)), '')
        shown = next((text for text in dates if text and text != release), '')
        if shown.upper() != wanted or path in seen:
            continue
        seen.add(path)
        entries.append({'path': path, 'release_date': release or None})
    return entries


# av-wiki 女优页红框里要留下的两行。站点上别名那一行的字段名是「別名義」
AVWIKI_ORIGIN = 'https://av-wiki.net'
AVWIKI_NAME_LABELS = ('AV女優名', '別名義', '別名')
_PROFILE_DASH = re.compile(r'\s+[–—―－-]\s+')
_PROFILE_PLACEHOLDERS = {'', '-', '—', '–', '―', '－', 'ー', 'なし', '無し', '不明', '未公開', '非公開'}


def avwiki_search_url(fanhao):
    '''按番号搜索。首页搜索框是 GET，参数名是 s'''
    from urllib.parse import urlencode
    return AVWIKI_ORIGIN + '/?' + urlencode({'s': same_code(fanhao)})


def _actress_url(href):
    absolute = urljoin(AVWIKI_ORIGIN + '/', href or '')
    parsed = urlparse(absolute)
    if '/av-actress/' not in parsed.path:
        return ''
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))


def parse_avwiki_cards(html, fanhao):
    '''
    搜索结果卡片上的女优：[(女优页地址, 卡片上她的名字)]，按卡片上的先后、同一个女优页只留一次。
    卡片用 fa-circle-o 那一行列出番号，番号对不上的卡片不要；女优在 li.actress-name 的链接里，链接的文字是她的名字
    （空白合成一个空格，没有文字时是空字符串）
    '''
    wanted = same_code(fanhao)
    if not wanted:
        return []
    soup = _soup(html)
    found = {}
    for article in soup.select('article.archive-list'):
        code = ''
        for icon in article.select('i.fa-circle-o'):
            row = icon.find_parent('li')
            if row is not None:
                code = row.get_text(' ', strip=True)
                break
        if code.upper() != wanted:
            continue
        for anchor in article.select('li.actress-name a[href]'):
            url = _actress_url(anchor['href'])
            if url and url not in found:
                found[url] = ' '.join(anchor.get_text(' ', strip=True).split())
    return list(found.items())


def parse_avwiki_search(html, fanhao):
    '''搜索结果卡片上的女优页链接（见 parse_avwiki_cards）'''
    return [url for url, _ in parse_avwiki_cards(html, fanhao)]


def _split_aliases(text):
    parts = []
    buf = []
    depth = 0
    for char in text or '':
        if char in '（(':
            depth += 1
        elif char in '）)' and depth:
            depth -= 1
        elif char in '、，' and depth == 0:
            parts.append(''.join(buf))
            buf = []
            continue
        buf.append(char)
    parts.append(''.join(buf))
    return parts


def names_from_profile_value(text):
    '''
    红框里一行名字拆开：括号外的名字、括号里的读音、破折号后面的罗马字。
    空的、占位的横线不要
    '''
    names = []
    for segment in _split_aliases(text):
        readings = [' '.join(item.split()) for item in re.findall(r'[（(]([^）)]*)[）)]', segment)]
        bare = re.sub(r'[（(][^）)]*[）)]', ' ', segment)
        pieces = [' '.join(part.split()) for part in _PROFILE_DASH.split(bare)]
        pieces = [part for part in pieces if part]
        ordered = []
        if pieces:
            ordered.append(pieces[0])
        ordered.extend(readings)
        ordered.extend(pieces[1:])
        for name in ordered:
            if name not in _PROFILE_PLACEHOLDERS:
                names.append(name)
    return names


def parse_avwiki_names(html):
    '''
    女优页红框：只要 AV女優名 和 別名義（页面上也可能写成 別名）。
    生年月日、尺寸、SNS 不取。没有 AV女優名 时说明页面结构对不上
    '''
    soup = _soup(html)
    profile = soup.select_one('div.actress-data dl')
    if profile is None:
        raise ParseError('女优页没有名字栏')
    values = {}
    for term in profile.find_all('dt'):
        label = term.get_text(strip=True).rstrip('：:')
        cell = term.find_next_sibling('dd')
        if cell is None or label not in AVWIKI_NAME_LABELS or label in values:
            continue
        values[label] = cell.get_text(' ', strip=True)
    if 'AV女優名' not in values:
        raise ParseError('女优页没有 AV女優名')
    names = []
    for label in AVWIKI_NAME_LABELS:
        for name in names_from_profile_value(values.get(label, '')):
            if name not in names:
                names.append(name)
    if not names:
        raise ParseError('女优页的名字是空的')
    return names
