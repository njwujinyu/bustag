%# 推荐页的三个标签页。tab 是当前页：like（推荐）/ dislike（不太像）/ pick；
%# pick_count 是符合设定的未打标影片数，没有设定时为 None
 <div class="row py-3">
	<div class="col-12">
		<ul class="nav nav-tabs">
		<li class="nav-item">
			<a class="nav-link {{'active' if tab == 'like' else ''}}" href="/?like=1">推荐</a>
		</li>
		<li class="nav-item">
			<a class="nav-link {{'active' if tab == 'dislike' else ''}}" href="/?like=0">不太像</a>
		</li>
		<li class="nav-item">
			<a class="nav-link {{'active' if tab == 'pick' else ''}}" href="/pick">指定推荐
			% if pick_count is not None:
			<span class="badge badge-pill badge-secondary">{{pick_count}}</span>
			% end
			</a>
		</li>
		</ul>
	</div>
</div>
