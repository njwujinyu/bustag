% rebase('base.tpl', title='确认', path=path)
%# 要先问一句的操作，没有经过页面脚本的确认框就提交过来时显示这一页（脚本还没加载好，或者浏览器关掉了脚本）。
%# question 是要问的话，detail 是补充说明；「确定」（文字是 yes）把 fields（[(名字, 值)]）加上 confirmed=1
%# 再提交到 action，「取消」回到 cancel

<div class="container">
 <div class="row py-3">
	<div class="col-12">
	<h5 id="confirm-question">{{question}}</h5>
	<p class="text-muted" id="confirm-detail">{{detail}}</p>
	<form id="confirm-form" action="{{action}}" method="post">
		% for name, value in fields:
		<input type="hidden" name="{{name}}" value="{{value}}">
		% end
		<input type="hidden" name="confirmed" value="1">
		<button type="submit" class="btn btn-danger btn-sm">{{yes}}</button>
		<a class="btn btn-outline-secondary btn-sm" href="{{cancel}}">取消</a>
	</form>
	</div>
 </div>
</div>
