'''
README 里和程序行为有关、容易说过头的几句：照程序现在的做法写
'''
from pathlib import Path

README = (Path(__file__).resolve().parent.parent / 'README.md').read_text(encoding='utf-8')


def line_with(text):
    found = [line for line in README.splitlines() if text in line]
    assert len(found) == 1, text
    return found[0]


def test_running_from_source_says_sqlite_needs_its_json_functions():
    line = line_with('需要 Python 3.11 – 3.14')
    assert 'JSON 函数' in line and '3.38' in line


def test_the_tidy_up_says_some_places_are_right_only_after_the_correction():
    # 迁移离线做，分不清是哪一位的名字（夹在两位女优之间的）要等后台重新查 av-wiki 才定得下来
    line = line_with('整理以后每部影片上还是原来那几位女优')
    assert '个别拿不准的地方要等联网校正之后才对' in line


def test_names_written_during_a_downgrade_to_0_3_are_not_said_to_be_tidied():
    # 0.3.x 补名时不记补过哪一部，换回新版本以后认不出它写下的名字
    line = line_with('用旧版本打开整理过的')
    assert '0.3.x' in line and '不会再整理' in line


def test_a_busy_database_does_not_claim_thirty_seconds_and_covers_one_held_exclusively():
    # SQLite 等锁按名义时长累计，实际常是 30 秒的两倍多；老库被别的程序独占时连读都读不了，马上就报
    assert '等了 30 秒' not in README
    line = line_with('连读都读不了')
    assert '独占' in line and '关掉' in line


def test_taking_back_a_page_says_its_queue_stays_while_another_actress_is_linked_to_it():
    # 取消关联时，演员页还关联着别的女优（或者按名字查找到它的人）的话，排队的影片留着
    line = line_with('点错了可以点**换一个**')
    assert '这个演员页还没抓的排队也不抓了' in line
    assert '还有别的女优也关联着这个演员页时，它排着的影片留着' in line


def test_a_busy_database_says_the_kept_clicks_go_on_by_themselves_and_how_soon():
    # 数据库被占着没做成的补全、查看、查找：程序自己隔几分钟再试，空出来以后最迟约 3 分钟接着做，不用等下一次定时抓取
    line = line_with('*数据库被别的程序占着*')
    assert '不用再点，程序自己接着试' in line and '最迟约 3 分钟' in line
    assert '每 3 分钟一次' in line and '不会再对网站发请求' in line
    assert '下一次定时抓取' not in line and '你下一次点击' not in line


def test_a_busy_database_says_who_sends_no_requests_and_that_the_scheduled_crawl_still_reads_the_home_page():
    # 不发请求的是留着的补全、查看、查找和排着的影片；定时抓取照常去读首页，每个抓取间隔一次
    line = line_with('*数据库被别的程序占着*')
    assert '留着的补全、查看、查找和排着的影片不会再对网站发请求' in line
    assert '定时抓取照常去读首页' in line


def test_a_busy_database_says_how_many_warnings_the_log_gets_whatever_the_crawl_does_meanwhile():
    # 头一次没做成一条、改成每隔几分钟再试一条；定时抓取在这期间撞上库忙不多写
    line = line_with('*数据库被别的程序占着*')
    assert '"抓排队的影片没有做成"这件事只写两条警告' in line and '定时抓取撞上几次' in line


def test_a_busy_database_says_a_cancel_made_meanwhile_does_not_take_effect():
    # 库被占着时点"取消排队"：等不到写锁，半分钟左右以后得到"数据库正忙"的页面，这次取消没有生效，要等库空出来再点
    line = line_with('*数据库被别的程序占着*')
    assert '半分钟左右' in line and '"数据库正忙"' in line
    assert '这次取消没有生效' in line and '空出来以后再点一次' in line


def test_the_repository_is_not_called_a_fork_and_the_original_author_is_still_thanked():
    # 发布时这个仓库重新建成一个独立的仓库（不再是 GitHub 上的 fork，也不带原仓库的提交历史）
    line = line_with('本仓库（[njwujinyu/bustag]')
    assert '是在原作者 0.2.1 的代码基础上继续维护的版本' in line
    assert 'fork' not in README.lower()
    assert '原作者 [gxtrobot](https://github.com/gxtrobot/bustag) 在 2019-10 发布 0.2.1 后停止了维护' in README
    assert '感谢原作者 gxtrobot 的创作，许可证（MIT）保持不变，见 [LICENSE](./LICENSE)' in README


def test_cancelling_the_queue_says_what_becomes_of_the_request_already_on_its_way():
    # 取消时已经发出去、正在等答复的请求照常处理：详情页入库，列表页读到了也不再往队里排
    line = line_with('**取消排队**去掉她还没抓的那些')
    assert '正在等网站答复的那一个照常处理' in line
    assert '抓到的那一部照常入库，读到的那一页不再往队里排' in line
