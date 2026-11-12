% rebase('base.tpl', title='打标', path=path, msg=msg, errmsg=errmsg)
% from urllib.parse import quote
% curr_page = page_info[2]
% q = q or ''
% q_attr = ('&q=' + quote(q, safe='')) if q else ''
%# like 是所在的标签页（喜欢 1、不喜欢 0）；"未打标的"不是哪个标签页，地址里不带它
% like_attr = '' if like is None else '&like={}'.format(like)
% if q:
%   empty_text = '没有匹配的结果'
% end
<div class="container">
 <div class="row py-3">
	<div class="col-12">
		<ul class="nav nav-tabs">
		<li class="nav-item">
			<a class="nav-link {{'active' if like is None else ''}}" href="{{'?' + q_attr[1:] if q_attr else '?'}}">未打标的</a>
		</li>
		<li class="nav-item">
			<a class="nav-link {{'active' if like==1 else ''}}" href="?like=1{{q_attr}}">喜欢</a>
		</li>
		<li class="nav-item">
			<a class="nav-link {{'active' if like==0 else ''}}" href="?like=0{{q_attr}}">不喜欢</a>
		</li>
		</ul>
	</div>
</div>
 <div class="row pb-3">
	<div class="col-12">
		<form class="form-inline" method="get" action="/tagit">
			% if like is not None:
			<input type="hidden" name="like" value="{{like}}">
			% end
			<label class="mr-2 mb-0" for="tag-search">搜索</label>
			<input id="tag-search" class="form-control form-control-sm mr-2" type="search" name="q" value="{{q}}" placeholder="可以搜索番号、标题、演员、标签">
			<button class="btn btn-outline-secondary btn-sm" type="submit">搜索</button>
		</form>
	</div>
</div>
%#generate list of rows of items
% i = 1
%for item in items:
%# 打标后回到 #form-N：编号放在整张卡片这一行上，回来时卡片的上边（封面）在视口里，不是只看到按钮
	<div class="row py-3" id="form-{{i}}">
		<div class="col-12 col-md-4">
% include('cover.tpl', fanhao=item.fanhao)
		</div>

			<div class="col-7 col-md-5">
			<div class="small text-muted">id: {{item.id}}</div>
			<div class="small text-muted">发行日期: {{item.release_date}}</div>
			<div class="small text-muted">添加日期: {{item.add_date}}</div>
			<h6>{{item.fanhao}} </h6>
			<a href="{{item.url}}" target="_blank" rel="noreferrer noopener"> {{item.title[:30]}} </a>
			<div class="item-tags">
			% for t in item.tags_dict['genre']:
			<span class="badge badge-primary">{{t}}</span>
			% end
			</div>
			<div class="item-tags">
			% for t in item.tags_dict['star']:
			<span class="badge badge-warning">{{t}}</span>
			% end
			</div>
			% if item.other_entries:
			<p class="small mb-1">这个番号还有其他条目：</p>
			% for entry in item.other_entries:
			<form action="/entry/{{item.fanhao}}" method="post" class="mb-1">
				<input type="hidden" name="back" value="tagit">
				<input type="hidden" name="page" value="{{curr_page}}">
				<input type="hidden" name="like" value="{{'' if like is None else like}}">
				<input type="hidden" name="q" value="{{q}}">
				<input type="hidden" name="path" value="{{entry.path}}">
				<button type="submit" class="btn btn-outline-secondary btn-sm" name="use" value="1">改选 {{entry.path}} {{entry.release_date or ''}}</button>
			</form>
			% end
			% end
			<form action="/entry/{{item.fanhao}}" method="post" class="form-inline mb-1">
				<input type="hidden" name="back" value="tagit">
				<input type="hidden" name="page" value="{{curr_page}}">
				<input type="hidden" name="like" value="{{'' if like is None else like}}">
				<input type="hidden" name="q" value="{{q}}">
				<input class="form-control form-control-sm" name="path" placeholder="/番号_日期">
				<button type="submit" class="btn btn-outline-secondary btn-sm" name="use" value="1">改用这个详情</button>
			</form>
			<form action="/entry/{{item.fanhao}}" method="post">
				<input type="hidden" name="back" value="tagit">
				<input type="hidden" name="page" value="{{curr_page}}">
				<input type="hidden" name="like" value="{{'' if like is None else like}}">
				<input type="hidden" name="q" value="{{q}}">
				<button type="submit" class="btn btn-link btn-sm" name="find" value="1">查找其他条目</button>
			</form>

			</div>
		<div class="col-5 col-md-3  align-self-center">
		<form action="/tag/{{item.fanhao}}?page={{curr_page}}{{like_attr}}{{q_attr}}" method="post">
		<input type=hidden name="formid" value="form-{{i}}">
% if like is None or like == 0:
		<button type="submit" name="submit" class="btn btn-primary btn-sm" value="1">喜欢</button>
% end
% if like is None or like == 1:
		<button type="submit" name="submit" class="btn btn-danger btn-sm" value="0">不喜欢</button>
% end
		</form>
		</div>
	</div>
% i = i + 1
%end
% if like is None:
% include('dislike_rest.tpl', items=items, back='tagit', page=curr_page, like='', q=q)
% end
% include('pagination.tpl', page_info=page_info)

</div>