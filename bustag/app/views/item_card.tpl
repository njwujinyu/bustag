%# 影片卡片的封面列和信息列，放在 <div class="row"> 里，后面由各页接上自己的按钮列。
%# 模型给这部影片打过分时（item.score 不是 None）在标签下面写相似度和影响分数最多的标签：
%# 在"推荐"里的（系统评分为喜欢）相似度是绿色的，其余是灰色的，并且再写出拉低分数的标签
		<div class="col-12 col-md-4">
% include('cover.tpl', fanhao=item.fanhao)
		</div>

			<div class="col-7 col-md-5">
			<div class="small text-muted">id: {{item.id}}</div>
			<div class="small text-muted">发行日期: {{item.release_date}}</div>
			<div class="small text-muted">添加日期: {{item.add_date}}</div>
			<h6>{{item.fanhao}} </h6>
			<a href="{{item.url}}" target="_blank" rel="noreferrer noopener"> {{item.title[:30]}} </a>
%# item-tags 里的徽标可以折行：名字很长时留在这一列里，不伸到按钮那一列下面
			<div class="item-tags">
			% for t in item.tags_dict['genre']:
			<span class="badge badge-primary">{{t}}</span>
			% end
			</div>
			<div class="item-tags">
			% for t in item.tags_dict['star']:
			<span class="badge badge-warning">{{t}}</span>
			% end
			</div>
			% if item.score is not None:
			% recommended = item.rate_value == 1
			<div class="small item-score">
			<span class="badge {{'badge-success' if recommended else 'badge-secondary'}}">相似度 {{'%.0f' % (item.score * 100)}}%</span>
			% if item.because:
			<span class="text-muted item-because">因为：{{'、'.join(item.because)}}</span>
			% end
			% if item.against and not recommended:
			<span class="text-muted item-against">拉低分数的：{{'、'.join(item.against)}}</span>
			% end
			</div>
			% end

			</div>
