% curr_page = page_info[2]
% max_page = page_info[1]
% total_items = page_info[0]
% setdefault('like', '')
<div class="row">
	<div class="col-12 text-center">
	% if total_items == 0:
	<h6>暂无数据</h6>
	% else:
	<h6>
	% if curr_page != 1:
	<a href="?page=1&like={{like}}"> 第一页</a>
	% end
	% if curr_page > 1:
	<a href="?page={{curr_page - 1}}&like={{like}}"> 上一页</a>
	% end
	第{{curr_page}}页
	% if curr_page < max_page:
	 <a href="?page={{curr_page + 1}}&like={{like}}">下一页</a>
	% end
	% if curr_page != max_page:
	<a href="?page={{max_page}}&like={{like}}">最后页</a>
	% end
	</h6>
	<div>
	<form>
		<span>共  {{max_page}}页,{{total_items}}条</span>
	跳转
	<select id="pagenav">
%# 页数很多时下拉框只列第一页、最后一页和当前页前后各 50 页
% nearby = range(max(1, curr_page - 50), min(max_page, curr_page + 50) + 1)
% for i in sorted({1, max_page, *nearby}):
% url = '?page={}&like={}'.format(i, like)
% selected = "selected" if i == curr_page else ""
	<option {{selected}} value="{{url}}">{{i}}</option>
% end
	</select>
	页
	</form>
	</div>
	% end
	</div>
</div>