% rebase('base.tpl', title='指定推荐', path='/', msg=msg, errmsg=errmsg)
% curr_page = page_info[2]
%# 女优和标签两部分的写法一样：类别、叫法、已选的名字、每个名字在库里的部数、喜欢的影片里可选的、输入框的提示
% rows = [('star', '女优', settings.stars, star_counts, liked_stars, '女优名字，多个用逗号分隔')]
% rows.append(('tag', '标签', settings.tags, tag_counts, liked_tags, '类别、系列、制作商等，多个用逗号分隔'))

<div class="container">
% include('recommend_tabs.tpl', tab='pick', pick_count=pick_count)
 <div class="row">
	<div class="col-12">
	<div class="card" id="pick-settings">
	<div class="card-body">
		<h5 class="card-title">推荐设定</h5>
		<p class="card-text small text-muted">在已经抓到的影片里，按下面指定的女优和标签，列出你还没打标的。不用先训练模型，也不会改动已有的打标。</p>
% for kind, word, names, counts, liked, hint in rows:
		<h6>指定{{word}}（{{len(names)}} / {{max_names}}）</h6>
		% if names:
		<form id="pick-chosen-{{kind}}" action="/pick/remove" method="post" class="pick-chips">
			<input type="hidden" name="kind" value="{{kind}}">
			% for name in names:
			<span class="badge badge-pill {{'badge-danger' if counts[name] == 0 else 'badge-light border'}} pick-chip">{{name}}
				<span class="pick-count">{{counts[name]}} 部</span>
				<button type="submit" name="name" value="{{name}}" class="pick-remove" title="去掉" aria-label="去掉 {{name}}">&times;</button>
			</span>
			% end
		</form>
		% if 0 in counts.values():
		<p class="small text-danger pick-missing">红色的是库里还没有带这个名字的影片，请核对写法（简体、繁体都可以；日文的写法要和网站上的一样）</p>
		% end
		% if kind == 'star':
		% for note in alias_notes:
		<p class="small text-muted mb-1 pick-alias">{{note}}</p>
		% end
		% end
		% else:
		<p class="small text-muted">还没有指定{{word}}</p>
		% end
		<form id="pick-add-{{kind}}" action="/pick/add" method="post" class="form-inline mb-3">
			<input type="hidden" name="kind" value="{{kind}}">
			<label class="sr-only" for="pick-{{kind}}-names">要添加的{{word}}</label>
			%# 联想由页面脚本列在输入框下面的 <ul> 里；没有脚本时它一直是空的、不显示
			<span class="pick-suggest mr-2 mb-1">
				<input id="pick-{{kind}}-names" class="form-control form-control-sm pick-names" type="text" name="names" autocomplete="off" data-suggest="{{kind}}" placeholder="{{hint}}">
				<ul id="pick-{{kind}}-list" class="pick-suggest-list" aria-label="{{word}}的联想" tabindex="-1" hidden></ul>
			</span>
			<button type="submit" class="btn btn-outline-primary btn-sm mb-1">添加</button>
		</form>
% end
% if liked_stars or liked_tags:
		<h6>从你喜欢的影片里选</h6>
		<p class="small text-muted mb-1">你打成喜欢的影片里出现最多的女优和标签，数字是出现在几部里，点一下就加入</p>
		% for kind, word, names, counts, liked, hint in rows:
		% if liked:
		<form id="pick-liked-{{kind}}" action="/pick/add" method="post" class="pick-chips">
			<input type="hidden" name="kind" value="{{kind}}">
			<span class="small text-muted mr-2 mb-1">{{word}}</span>
			% for name, count in liked:
			<button type="submit" name="name" value="{{name}}" class="btn btn-outline-secondary btn-sm pick-choice">{{name}} <span class="badge badge-light">{{count}}</span></button>
			% end
		</form>
		% end
		% end
% end
		<h6>条件关系</h6>
		<form id="pick-options" action="/pick/options" method="post">
			<div class="form-check form-check-inline">
				<input class="form-check-input" type="radio" name="mode" id="pick-mode-all" value="all" {{'checked' if settings.mode == 'all' else ''}}>
				<label class="form-check-label" for="pick-mode-all">同时满足</label>
			</div>
			<div class="form-check form-check-inline">
				<input class="form-check-input" type="radio" name="mode" id="pick-mode-any" value="any" {{'checked' if settings.mode == 'any' else ''}}>
				<label class="form-check-label" for="pick-mode-any">满足任一</label>
			</div>
			<ul class="small text-muted pl-3 my-2">
				<li>同时满足：选了女优，影片里要有其中至少一位；选了标签，每个标签都要有。</li>
				<li>满足任一：有任意一位所选女优，或者带任意一个所选标签就算。</li>
				<li>例如指定女优 A、B 和标签 X、Y：同时满足列出 A 或 B 出演、并且 X 和 Y 都带的影片；满足任一列出 A 的、B 的、带 X 的、带 Y 的全部影片。</li>
			</ul>
			<div class="form-check mb-2">
				<input class="form-check-input" type="checkbox" name="model_only" id="pick-model-only" value="1" {{'checked' if settings.model_only else ''}}>
				<label class="form-check-label" for="pick-model-only">只看在「推荐」里的</label>
			</div>
			% if model_idle:
			<p class="small text-danger pick-model-idle">库里还没有模型的推荐结果（还没训练模型，或者训练后还没打过分），这个选项暂时不起作用</p>
			% end
			<button type="submit" class="btn btn-primary btn-sm">保存</button>
		</form>
		<form id="pick-clear" action="/pick/clear" method="post" class="mt-2" data-confirm="确定清空推荐设定吗？已选的女优和标签都会去掉">
			<button type="submit" class="btn btn-outline-danger btn-sm">清空设定</button>
		</form>
	</div>
	</div>
% include('backfill.tpl', backfill=backfill, paused_text=paused_text)
	</div>
 </div>
% if not settings.is_empty:
 <div class="row pt-3">
	<div class="col-12">
		<h6 id="pick-total">{{'符合设定、也在「推荐」里的未打标影片' if model_active else '符合设定的未打标影片'}}共 {{page_info[0]}} 部</h6>
		% if list_note or list_error:
		<div class="alert {{'alert-success' if list_note else 'alert-danger'}} mt-2 mb-0" role="alert" id="pick-done">{{list_note or list_error}}</div>
		% end
	</div>
 </div>
% end
% i = 1
% for item in items:
%# 打标后回到 #form-N：编号放在整张卡片这一行上，回来时卡片的上边（封面）在视口里，不是只看到按钮
	<div class="row py-3" id="form-{{i}}">
% include('item_card.tpl', item=item)
		<div class="col-5 col-md-3  align-self-center">
		<form action="/tag/{{item.fanhao}}?page={{curr_page}}" method="post">
		<input type="hidden" name="back" value="pick">
		<input type=hidden name="formid" value="form-{{i}}">
		<button type="submit" name="submit" class="btn btn-primary btn-sm" value="1">喜欢</button>
		<button type="submit" name="submit" class="btn btn-danger btn-sm" value="0">不喜欢</button>
		</form>
		</div>
	</div>
% i = i + 1
% end
% if settings.is_empty:
 <div class="row py-3">
	<div class="col-12 text-center">
	<h6 id="pick-empty">还没有设定：在上面的<a href="#pick-settings">推荐设定</a>里添加女优或标签，这里就会列出符合的影片</h6>
	</div>
 </div>
% else:
% include('dislike_rest.tpl', items=items, back='pick', page=curr_page, like='', q='')
% include('pagination.tpl', page_info=page_info, like='', q='', anchor='#pick-total', empty_text='暂时没有符合设定的未打标影片，抓到新影片后会自动出现在这里')
% end

</div>