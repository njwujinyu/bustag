%# 一张封面。<img> 上先不写真正的地址（src 是一张 1×1 的透明图），由页面脚本排队加载，一次最多同时取 2 张：
%# 封面没缓存时每个请求都要等服务端去下载，全部一起发会占满浏览器对同一站点的连接，页面上别的请求只能排在后面。
%# 关掉脚本时显示 <noscript> 里的那一张（base.tpl 里有一条样式把上面那张藏起来）
		<img class="img-fluid img-thumbnail coverimg cover-waiting" src="{{blank_image}}" data-cover="/cover/{{fanhao}}" alt="{{fanhao}} 封面">
		<noscript><img class="img-fluid img-thumbnail" src="/cover/{{fanhao}}" loading="lazy" alt="{{fanhao}} 封面"></noscript>
