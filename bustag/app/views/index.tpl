% rebase('base.tpl', title='推荐', path=path, msg=msg, notice=notice, done=done, errmsg=errmsg)
% curr_page = page_info[2]

<div class="container">
% include('recommend_tabs.tpl', tab='like' if like == 1 else 'dislike', pick_count=pick_count)
 <div class="row">
	<div class="col-12">
	<p class="small text-muted mb-0" id="recommend-rule">模型给每部还没打标的影片打一个相似度（和你喜欢的影片有多像）。「推荐」是相似度最高的 {{recommended_percent}}%（至少 {{min_recommended}} 部），其余在「不太像」里，两边都按相似度从高到低排。点"喜欢"或"不喜欢"就是打标。</p>
	</div>
 </div>
%#generate list of rows of items
% i = 1
%for item in items:
<form id="form-{{i}}" action="/tag/{{item.fanhao}}?page={{curr_page}}&like={{like}}" method="post">
	<div class="row py-3">
% include('item_card.tpl', item=item)
		<div class="col-5 col-md-3  align-self-center">
		<input type="hidden" name="back" value="recommend">
		<input type=hidden name="formid" value="form-{{i}}">
		<button type="submit" name="submit" class="btn btn-primary btn-sm" value="1">喜欢</button>
		<button type="submit" name="submit" class="btn btn-danger btn-sm" value="0">不喜欢</button>
		</div>
	</div>
	</form>
% i = i + 1
%end
% include('dislike_rest.tpl', items=items, back='recommend', page=curr_page, like=like, q='')
% include('pagination.tpl', page_info=page_info)

</div>
