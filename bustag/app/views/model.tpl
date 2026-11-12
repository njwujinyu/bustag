% rebase('base.tpl', title='模型', path=path, msg=msg, notice=notice)
%# model 是当前模型的各项数字（index._model_view），没有可用的模型时为 None；labels 是"现在有多少条打标"那一句；
%# replacing 不为空表示旧版本的模型正在被自动重新训练，内容是要显示的那句话

<div class="container">
 <div class="row py-3">
 <div class="col-10 offset-1 ">
%# 外层不用 Bootstrap 的 accordion 类：它给第一张卡片去掉下边框，等着和下一张连在一起，这里只有一张
    <div id="accordionExample">
  <div class="card">
    <div class="card-header" id="headingOne">
      <h2 class="mb-0">
        <button class="btn btn-link" type="button" data-toggle="collapse" data-target="#collapseOne" aria-expanded="true" aria-controls="collapseOne">
          训练模型
        </button>
      </h2>
    </div>

    <div id="collapseOne" class="collapse show" aria-labelledby="headingOne" data-parent="#accordionExample">
        <div class="card-body">
        <h5 class="card-title">重新训练模型</h5>
        <p class="card-text">用全部打标数据重新训练模型，训练完马上给还没打标的影片重新打分。打标增多以后重新训练，推荐会更准</p>
        <p class="card-text" id="model-labels">{{labels}}</p>
        <form action="/do-training" method="post">
        <button type="submit" class="btn btn-primary">开始训练</button>
        </form>
        % if training_error:
        <p class="card-text text-danger mt-3">训练失败：{{training_error}}</p>
        % end
        </div>
        <div class="card-header">
           <h6> 当前模型数据 </h6>
        </div>
        % if error_msg:
        <p class="card-text text-danger">{{error_msg}} </p>
        % end
        % if replacing:
        <div class="card-body">
        <p class="card-text" id="model-upgrading">{{replacing}}</p>
        </div>
        % end
        % if model is not None:
        <div class="card-body" id="model-report">
        <p class="card-text">用了 {{model['likes'] + model['dislikes']}} 条打标（喜欢 {{model['likes']}}、不喜欢 {{model['dislikes']}}）训练，训练时间 {{model['trained_at']}}。</p>
        <p class="card-text">交叉验证（把打标分成 {{model['folds']}} 份，轮流拿 1 份出来检验，重复 {{model['repeats']}} 次）：{{model['major']}}认对 {{model['major_recall']}}%，{{model['minor']}}认对 {{model['minor_recall']}}%（全部猜{{model['major']}}时是 0%）。</p>
        <p class="card-text">按相似度排序时，喜欢的影片排在不喜欢的影片前面的概率是 {{model['auc']}}%（乱排是 50%）。</p>
        % if model['minor_count'] < model['target']:
        <p class="card-text">{{model['minor']}}只有 {{model['minor_count']}} 条，"{{model['minor']}}认对 {{model['minor_recall']}}%"这个数字上下可能差 {{model['margin']}} 个百分点；{{model['minor']}}攒到 {{model['target']}} 条以上后重新训练会更准（还差 {{model['target'] - model['minor_count']}} 条）。</p>
        % else:
        <p class="card-text">{{model['minor']}}有 {{model['minor_count']}} 条，"{{model['minor']}}认对 {{model['minor_recall']}}%"这个数字上下可能差 {{model['margin']}} 个百分点。</p>
        % end
        % if model['alike']:
        <p class="card-text" id="model-alike">还没打标的影片里，{{model['major_side']}} / {{model['scored']}} 部的相似度都{{'过半' if model['major'] == '喜欢' else '不到一半'}}：它们大多和你{{model['major']}}的影片很像，不是模型分不开。「推荐」里仍是其中相似度最高的那一部分。</p>
        % end
        % if model['tentative']:
        <div class="alert alert-warning" role="alert">"{{model['minor']}}"的打标不到 {{model['solid']}} 条，这个模型的结果仅供参考。</div>
        % end
        % if model['guessing']:
        <div class="alert alert-danger" role="alert">现在的模型把所有影片都判成{{model['major']}}，等于没有筛选。原因通常是"{{model['minor']}}"的打标太少：多标一些"{{model['minor']}}"的影片，再重新训练。</div>
        % end
        </div>
        % elif not error_msg and not replacing:
        <div class="card-body">
           还没有训练过模型.
        </div>
        % end
        </div>
    </div>
  </div>
 </div>
 </div>
</div>
