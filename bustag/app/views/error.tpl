% rebase('base.tpl', title=title, path=path)

<div class="container">
 <div class="row py-3">
	<div class="col-10 offset-1">
		<h4>{{status}} {{title}}</h4>
		<p>{{reason}}</p>
		% if home:
		<p><a href="/">返回首页</a></p>
		% end
	</div>
 </div>
</div>
