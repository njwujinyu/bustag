% rebase('base.tpl', title='关于', path=path)
%# tasks 是后台任务现在的状态：[(名称, 一段说明的文字)]

<div class="container">
<div class="row py-3">
    <div class="col-10 offset-1 ">
        <div class="text-center">
        <h2>免责声明</h2>
本软件仅用于技术学习使用，禁止用于商业用途，使用本软件所造成的后果由使用者承担！
        </div>
    </div>
</div>

<div class="row py-3">
    <div class="col-10 offset-1 ">
        <div class="text-center">
        <h2>关于本项目</h2>
Bustag 原作者为 <a href="https://github.com/gxtrobot/bustag" target="_blank" rel="noreferrer noopener">gxtrobot</a>，原项目已停止维护。当前维护版本为 <a href="https://github.com/njwujinyu/bustag" target="_blank" rel="noreferrer noopener">https://github.com/njwujinyu/bustag</a>，问题请到该仓库的 Issues 反馈。
        </div>
    </div>
</div>

<div class="row py-3">
    <div class="col-10 offset-1 ">
        <h2 class="text-center">后台任务</h2>
        <dl id="background-tasks">
        % for name, text in tasks:
            <dt>{{name}}</dt>
            <dd>{{text}}</dd>
        % end
        </dl>
    </div>
</div>

</div>
