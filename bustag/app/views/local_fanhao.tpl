% rebase('base.tpl', title='本地', path=path, msg=msg, errmsg=errmsg)
%# queue 是上传番号、导入打标库的下载队列（backfill.code_queue()）：还在排队的个数，没抓到的番号（新排进来的在前）；
%# reason_length 是原因最多显示的字数（网络库的报错常常很长，完整的在运行日志里）

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

<form action="" method="post">
	<div class="row py-3">
		<div class="col-12 ">
		<div class="form-check">
			<input class="form-check-input" type="checkbox" value="1" id="tag_like" name="tag_like">
			<label class="form-check-label" for="tag_like">
				全部打标为喜欢
			</label>
		</div>
		  <div class="form-group py-3">
			<label for="fanhao">每行格式: 番号(XXX-123),URL(可省略, 本地文件无效, 须为Plex等服务器视频URL)</label>
			<textarea class="form-control" id="fanhao" name="fanhao" rows="20"></textarea>
		  </div>
		<div class="text-center">
		<button type="submit" name="submit" class="btn btn-primary mx-2" value="1">提交</button>
		<button type="reset" class="btn btn-danger">重置</button>
		</div>
		</div>
	</div>
</form>

% if queue.waiting or queue.problems:
 <div class="row pb-3" id="code-queue">
	<div class="col-12">
		<h6>下载队列</h6>
		<p class="small text-muted mb-2">库里还没有的番号排在这里，由后台一个一个地抓，抓完自动打分；程序关掉再打开会接着抓。</p>
		% if queue.waiting:
		<p class="small mb-2">还在排队 {{queue.waiting}} 个番号。</p>
		% end
		% if queue.problems:
		<p class="small mb-1">没有抓到的 {{len(queue.problems) + queue.more}} 个（"网站上没有"是网站答复没有这一部；"失败"的可以再试）：</p>
		<ul class="list-unstyled small code-problems">
			% for task in queue.problems:
			<li class="code-problem">
				<span class="code-fanhao font-weight-bold mr-2">{{task.fanhao}}</span>
				<span class="code-result mr-2 {{'text-muted' if task.status == 'missing' else 'text-danger'}}">{{'网站上没有' if task.status == 'missing' else '失败'}}</span>
				<span class="code-reason text-muted mr-2">{{task.detail if len(task.detail) <= reason_length else task.detail[:reason_length] + '…'}}</span>
				% if task.tried_at:
				<span class="code-time text-muted mr-2">{{task.tried_at.strftime('%Y-%m-%d %H:%M')}}</span>
				% end
				% if task.status == 'failed':
				<form action="/local_fanhao/retry" method="post" class="d-inline">
					<input type="hidden" name="fanhao" value="{{task.fanhao}}">
					<button type="submit" class="btn btn-link btn-sm p-0 align-baseline">再试</button>
				</form>
				% end
			</li>
			% end
		</ul>
		% if queue.more:
		<p class="small text-muted">还有 {{queue.more}} 个没有列出来。</p>
		% end
		<form action="/local_fanhao/retry" method="post" class="d-inline">
			<input type="hidden" name="all" value="1">
			<button type="submit" class="btn btn-outline-primary btn-sm">失败的全部再试</button>
		</form>
		<form action="/local_fanhao/clear" method="post" class="d-inline" data-confirm="确定清除这些没有抓到的记录吗？番号不会再列在这里，以后可以重新上传">
			<button type="submit" class="btn btn-outline-secondary btn-sm">清除这些记录</button>
		</form>
		% end
	</div>
 </div>
% end

</div>
