'''
Windows exe 冒烟用的合成数据，只在 CI 里生成，不进 zip、镜像和安装包。
在 BUSTAG_DATA_DIR 指定的数据目录里写入影片和用户打标；"喜欢"的影片都带同一个类别和演员，训练出来的模型能区分两类。

  python .github/scripts/smoke_data.py seed --rated 240 --unrated 40   # 用户打标的影片（喜欢、不喜欢各一半）和未打标的影片
  python .github/scripts/smoke_data.py seed --unrated 30               # 再补一批未打标的影片
  python .github/scripts/smoke_data.py status                          # 打印"还没有评分的影片数 系统推荐数"
'''
import argparse
import os
import random
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

PREFIX = 'SMOKE'
GENRES = [f'类别{i:02d}' for i in range(1, 21)]
STARS = [f'演员{i:02d}' for i in range(1, 31)]
LIKED_GENRE = '类别00'
LIKED_STAR = '演员00'


def seed(rated, unrated):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from bustag.spider import db
    from bustag.spider.parser import Tag

    start = db.Item.select().where(db.Item.fanhao.startswith(PREFIX + '-')).count()
    rng = random.Random(start)
    with db.db.atomic():
        for n in range(rated + unrated):
            fanhao = f'{PREFIX}-{start + n + 1:04d}'
            like = n % 2 == 0
            genres = rng.sample(GENRES, 3) + ([LIKED_GENRE] if like else [])
            stars = rng.sample(STARS, 2) + ([LIKED_STAR] if like else [])
            tags = ([Tag('genre', genre, f'/genre/{genre}') for genre in genres]
                    + [Tag('star', star, f'/star/{star}') for star in stars])
            meta = {'fanhao': fanhao, 'title': f'冒烟影片 {fanhao}', 'url': f'/{fanhao}',
                    'release_date': '2026-01-01', 'cover_img_url': '', 'length': '120'}
            if db.save(meta, tags) is None:
                sys.exit(f'{fanhao} 已经在库里')
            if n < rated:
                db.ItemRate.saveit(db.RATE_TYPE.USER_RATE, int(like), fanhao)
    print(f'已写入 {rated + unrated} 部影片（用户打标 {rated} 部，未打标 {unrated} 部），'
          f'库里共 {db.Item.select().count()} 部')


def status():
    # 只读数据库，不导入 bustag（导入时会初始化数据目录、写日志）；rate_type 2 是系统推荐
    with closing(sqlite3.connect(Path(os.environ['BUSTAG_DATA_DIR']) / 'bus.db')) as con:
        unrated = con.execute('SELECT COUNT(*) FROM item LEFT JOIN item_rate ON item_rate.item_id = item.fanhao '
                              'WHERE item_rate.id IS NULL').fetchone()[0]
        system = con.execute('SELECT COUNT(*) FROM item_rate WHERE rate_type = 2').fetchone()[0]
    print(unrated, system)


def main():
    parser = argparse.ArgumentParser(description='Windows exe 冒烟用的合成数据')
    commands = parser.add_subparsers(dest='command', required=True)
    seed_parser = commands.add_parser('seed', help='写入合成的影片和打标')
    seed_parser.add_argument('--rated', type=int, default=0, help='用户打标的影片数，喜欢、不喜欢各一半')
    seed_parser.add_argument('--unrated', type=int, default=0, help='未打标的影片数')
    commands.add_parser('status', help='打印还没有评分的影片数和系统推荐数')
    args = parser.parse_args()
    if not os.environ.get('BUSTAG_DATA_DIR'):
        sys.exit('请先设置环境变量 BUSTAG_DATA_DIR')
    if args.command == 'seed':
        seed(args.rated, args.unrated)
    else:
        status()


if __name__ == '__main__':
    main()
