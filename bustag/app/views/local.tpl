% rebase('base.tpl', title='本地文件', path=path, msg=msg, errmsg=errmsg)
% curr_page = page_info[2]

<div class="container">
 <div class="row py-3">
	<div class="col-12">
		<ul class="nav nav-tabs">
        <li class="nav-item">
			<a class="nav-link {{'active' if path=='/local' else ''}}" href="/local">本地文件</a>
		</li>
        <li class="nav-item">
			<a class="nav-link {{'active' if path=='/local_fanhao' else ''}}" href="/local_fanhao">上传番号</a>
		</li>
		<li class="nav-item">
		</li>
		</ul>
	</div>
 </div>
% if pending:
 <div class="row">
	<div class="col-12 small text-muted">还有 {{pending}} 个本地文件的影片信息没有下载到（正在下载，或者番号不存在、下载失败），暂不显示</div>
 </div>
% end
%#generate list of rows of items
%for local_item in items:
	<div class="row py-3">
		<div class="col-12 col-md-4">
% include('cover.tpl', fanhao=local_item.item.fanhao)
		</div>

			<div class="col-7 col-md-5">
			<div class="small text-muted">发行日期: {{local_item.item.release_date}}</div>
			<div class="small text-muted">上次观看: {{local_item.last_view_date}}</div>
			<div class="small text-muted">观看次数: {{local_item.view_times}}</div>
			<h6>{{local_item.item.fanhao}} </h6>
			<a href="{{local_item.item.url}}" target="_blank" rel="noreferrer noopener"> {{local_item.item.title[:30]}} </a>
			<div class="item-tags">
			% for t in local_item.item.tags_dict['genre']:
			<span class="badge badge-primary">{{t}}</span>
			% end
			</div>
			<div class="item-tags">
			% for t in local_item.item.tags_dict['star']:
			<span class="badge badge-warning">{{t}}</span>
			% end
			</div>
			% if local_item.item.other_entries:
			<p class="small mb-1">这个番号还有其他条目：</p>
			% for entry in local_item.item.other_entries:
			<form action="/entry/{{local_item.item.fanhao}}" method="post" class="mb-1">
				<input type="hidden" name="back" value="local">
				<input type="hidden" name="page" value="{{curr_page}}">
				<input type="hidden" name="path" value="{{entry.path}}">
				<button type="submit" class="btn btn-outline-secondary btn-sm" name="use" value="1">改选 {{entry.path}} {{entry.release_date or ''}}</button>
			</form>
			% end
			% end
			<form action="/entry/{{local_item.item.fanhao}}" method="post" class="form-inline mb-1">
				<input type="hidden" name="back" value="local">
				<input type="hidden" name="page" value="{{curr_page}}">
				<input class="form-control form-control-sm" name="path" placeholder="/番号_日期">
				<button type="submit" class="btn btn-outline-secondary btn-sm" name="use" value="1">改用这个详情</button>
			</form>
			<form action="/entry/{{local_item.item.fanhao}}" method="post">
				<input type="hidden" name="back" value="local">
				<input type="hidden" name="page" value="{{curr_page}}">
				<button type="submit" class="btn btn-link btn-sm" name="find" value="1">查找其他条目</button>
			</form>

			</div>
		<div class="col-5 col-md-3  align-self-center">
<a class="btn btn-primary" target="_blank" href="/local_play/{{local_item.id}}" role="button">播放</a>
		</div>
	</div>
%end
% include('pagination.tpl', page_info=page_info)

</div>