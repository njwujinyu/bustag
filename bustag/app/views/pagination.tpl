% curr_page = page_info[2]
% max_page = page_info[1]
% total_items = page_info[0]
% from urllib.parse import quote
% setdefault('like', '')
% setdefault('q', '')
% setdefault('empty_text', '暂无数据')
%# anchor 是翻页后要停在的位置（如 #pick-total），列表上面还有别的内容的页面用它，免得每翻一页都回到页面顶上
% setdefault('anchor', '')
%# like 是列表所在的标签页，没有标签页可分的列表（打标页的"未打标的"、指定推荐）链接里不带它
% like_attr = '' if like in ('', None) else '&like={}'.format(like)
% q_attr = ('&q=' + quote(str(q), safe='')) if q else ''
<div class="row">
	<div class="col-12 text-center">
	% if total_items == 0:
	<h6>{{empty_text}}</h6>
	% else:
	<h6>
	% if curr_page != 1:
	<a href="?page=1{{like_attr}}{{q_attr}}{{anchor}}"> 第一页</a>
	% end
	% if curr_page > 1:
	<a href="?page={{curr_page - 1}}{{like_attr}}{{q_attr}}{{anchor}}"> 上一页</a>
	% end
	第{{curr_page}}页
	% if curr_page < max_page:
	 <a href="?page={{curr_page + 1}}{{like_attr}}{{q_attr}}{{anchor}}">下一页</a>
	% end
	% if curr_page != max_page:
	<a href="?page={{max_page}}{{like_attr}}{{q_attr}}{{anchor}}">最后页</a>
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
% url = '?page={}{}{}{}'.format(i, like_attr, q_attr, anchor)
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