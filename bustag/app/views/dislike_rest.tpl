%# 列表下面的「这一页剩下的都不喜欢」。这一页列出的番号放在表单里一起提交：标的是用户看到的这些影片，
%# 不是提交时服务器算出来的那一页。back 是哪个列表页（recommend / pick / tagit），page 是页码，
%# like 是推荐页的标签页（别的页为空），q 是打标页的搜索词（没有时为空）。列表为空时不显示。
%# 提交前页面脚本用确认框问一句（data-confirm）；没有经过脚本的提交，服务端回一个确认页
% if items:
 <div class="row pb-3">
	<div class="col-12 text-center">
	<form id="dislike-rest" action="/dislike-page" method="post" data-confirm="确定把这一页的 {{len(items)}} 部影片都标为不喜欢吗？标错了可以到打标页的「不喜欢」里改回来">
		<input type="hidden" name="back" value="{{back}}">
		<input type="hidden" name="page" value="{{page}}">
		% if like != '':
		<input type="hidden" name="like" value="{{like}}">
		% end
		% if q:
		<input type="hidden" name="q" value="{{q}}">
		% end
		% for item in items:
		<input type="hidden" name="fanhao" value="{{item.fanhao}}">
		% end
		<button type="submit" class="btn btn-outline-danger btn-sm">这一页剩下的都不喜欢</button>
		<div class="small text-muted mt-1">把这一页的 {{len(items)}} 部都标为不喜欢，提交前会再问一句；已经打过标的不会改</div>
	</form>
	</div>
 </div>
% end
