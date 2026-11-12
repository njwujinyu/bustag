%# 指定推荐页上"按演员补全作品"那一块（index._backfill_view() 的结果放在 backfill 里，没有指定女优时是 None）。
%# 页面脚本在 data-refresh="1" 时每隔几秒取 /pick/backfill（就是这个模板）换掉整块；没有脚本时刷新页面也是一样的内容。
%# 一位女优一行。每个按钮都是一个表单，带的是这一行的第一个名字和这一行的键（一个名字是几位女优共用的别名时
%# 每人一行，靠键分清）；服务端按当前的设定重新找到她那一行
% if backfill is not None:
<div class="card mt-3" id="backfill" data-refresh="{{'1' if backfill.active else '0'}}">
<div class="card-body">
	<h5 class="card-title">按演员补全作品</h5>
	<p class="card-text small text-muted mb-1">从 JavBus 上她的演员页，把库里还没有的作品排进队列，后台一部一部地抓，抓完自动打分，会出现在这一页和「推荐」里。点了才抓：每次每位最多 {{backfill.batch}} 部，所有演员合计每天最多 {{backfill.daily}} 部；只补有磁力的（和首页一样），没有磁力的不排。</p>
	<p class="small mb-2" id="backfill-today">今天已抓 {{backfill.used}} 部，还可以抓 {{backfill.left}} 部{{'；现在排队 %d 部' % backfill.listed if backfill.listed else ''}}。
	% if backfill.listed and not backfill.left and not backfill.paused:
	今天的额度已经用完，排队的明天 {{backfill.tomorrow}} 起接着抓。
	% end
	% if backfill.busy:
	{{backfill.busy}}。
	% end
	</p>
	% if backfill.paused:
	<div class="alert alert-warning small py-2 mb-2" id="backfill-paused">{{paused_text(backfill.paused)}}
		<form action="/pick/backfill/resume" method="post" class="d-inline">
			<button type="submit" class="btn btn-link btn-sm p-0 align-baseline">现在继续</button>
		</form>
	</div>
	% end
	<ul class="list-unstyled backfill-rows mb-0">
		% for row in backfill.rows:
		<li class="backfill-row" data-name="{{row.name}}" data-row="{{row.row}}">
			<div><span class="backfill-names font-weight-bold">{{row.names}}{{'（%s）' % row.who if row.who else ''}}</span> <span class="small">库里 {{row.movies}} 部</span></div>
			% if row.linked:
			<div class="small backfill-linked">{{row.linked}}</div>
			% end
			<div class="small text-muted backfill-facts">{{row.facts}}</div>
			% if row.queue:
			<div class="small backfill-queue">{{row.queue}}</div>
			% end
			% if row.position:
			<div class="small text-muted backfill-position">{{row.position}}</div>
			% end
			% if row.doing:
			<div class="small text-primary backfill-doing">{{row.doing}}</div>
			% end
			% if row.error:
			<div class="small text-danger backfill-error">{{row.error}}</div>
			% end
			% if row.check or row.start or row.cancel or row.retry or row.search or row.candidates or row.relink:
			<div class="backfill-actions">
				% for path, label in row.candidates:
				<form action="/pick/backfill/choose" method="post"><input type="hidden" name="name" value="{{row.name}}"><input type="hidden" name="row" value="{{row.row}}"><input type="hidden" name="path" value="{{path}}"><button type="submit" class="btn btn-outline-primary btn-sm">{{label}}</button></form>
				% end
				% if row.search:
				<form action="/pick/backfill/search" method="post"><input type="hidden" name="name" value="{{row.name}}"><input type="hidden" name="row" value="{{row.row}}"><button type="submit" class="btn btn-outline-secondary btn-sm">{{row.search}}</button></form>
				% end
				% if row.check:
				<form action="/pick/backfill/check" method="post"><input type="hidden" name="name" value="{{row.name}}"><input type="hidden" name="row" value="{{row.row}}"><button type="submit" class="btn btn-outline-secondary btn-sm">{{row.check}}</button></form>
				% end
				% if row.start:
				<form action="/pick/backfill/start" method="post"><input type="hidden" name="name" value="{{row.name}}"><input type="hidden" name="row" value="{{row.row}}"><button type="submit" class="btn btn-outline-primary btn-sm">{{row.start}}</button></form>
				% end
				% if row.cancel:
				<form action="/pick/backfill/cancel" method="post" data-confirm="确定取消「{{row.names}}」还没抓的排队吗？已经抓到的留在库里"><input type="hidden" name="name" value="{{row.name}}"><input type="hidden" name="row" value="{{row.row}}"><button type="submit" class="btn btn-outline-danger btn-sm">取消排队</button></form>
				% end
				% if row.retry:
				<form action="/pick/backfill/retry" method="post"><input type="hidden" name="name" value="{{row.name}}"><input type="hidden" name="row" value="{{row.row}}"><button type="submit" class="btn btn-outline-secondary btn-sm">{{row.retry}}</button></form>
				% end
				% if row.relink:
				<form action="/pick/backfill/relink" method="post"><input type="hidden" name="name" value="{{row.name}}"><input type="hidden" name="row" value="{{row.row}}"><button type="submit" class="btn btn-outline-secondary btn-sm">换一个</button></form>
				<form action="/pick/backfill/unlink" method="post" data-confirm="确定这个 JavBus 演员页不是「{{row.names}}」吗？取消关联以后，这个演员页还没抓的排队也不抓了（还有别的女优也关联着这个演员页时，它排着的影片留着）"><input type="hidden" name="name" value="{{row.name}}"><input type="hidden" name="row" value="{{row.row}}"><button type="submit" class="btn btn-outline-danger btn-sm">取消关联</button></form>
				% end
			</div>
			% end
		</li>
		% end
	</ul>
</div>
</div>
% end
