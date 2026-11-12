$(function () {
    $('.coverimg').on('click', function () {
        // 还没加载出来的封面没有可放大的图
        if ($(this).hasClass('cover-waiting')) {
            return;
        }
        $('#imglarge').attr('src', $(this).attr('src'));
        $('#imagemodal').modal('show');
    });

    // 封面一次最多同时取 2 张，和服务端同时下载的张数一致。封面没缓存时每个请求都要等服务端去下载，
    // 一打开页面就全部发出去的话会占满浏览器对同一站点的 6 条连接，联想、打标这些请求只能排在后面
    var COVERS_AT_ONCE = 2;
    var waitingCovers = [];
    var loadingCovers = [];

    function loadNextCover() {
        var image = waitingCovers.shift();
        if (!image) {
            return;
        }
        loadingCovers.push(image);
        $(image).one('load error', function () {
            loadingCovers.splice(loadingCovers.indexOf(image), 1);
            $(image).removeClass('cover-waiting');
            loadNextCover();
        });
        image.src = image.getAttribute('data-cover');
    }

    // 还没加载出来的封面按页面上的先后排队
    function loadCovers() {
        waitingCovers = $('img.coverimg.cover-waiting').toArray();
        while (loadingCovers.length < COVERS_AT_ONCE && waitingCovers.length) {
            loadNextCover();
        }
    }

    loadCovers();

    // 翻页、点链接离开时，正在取的封面不要了。浏览器会把离开的页面连同它在途的请求留在"往返缓存"里，
    // 不取消的话这些请求还占着连接和服务端的线程，下一页的请求只能排在它们后面；
    // 去掉 src 浏览器就断开这个请求，服务端发现后不再为它排队
    $(window).on('pagehide', function () {
        waitingCovers = [];
        $(loadingCovers).off('load error').removeAttr('src');
        loadingCovers = [];
    });

    // 按"后退"从往返缓存回到这一页时，接着取还没加载出来的
    $(window).on('pageshow', function (event) {
        if (event.originalEvent.persisted) {
            loadCovers();
        }
    });

    $('#pagenav').on('change', function () {
        window.location = $(this).val();
    });

    // 带 data-confirm 的表单（清空设定、整页标不喜欢、取消排队）提交前用浏览器自带的确认框问一句，确定了就带上
    // confirmed=1。没带它的提交（这段脚本还没加载好，或者浏览器关掉了脚本），要确认的那几个服务端会先回一个确认页。
    // 挂在 document 上：按演员补全那一块会被整块换掉，里面的表单是后来才有的
    $(document).on('submit', 'form[data-confirm]', function () {
        if (!window.confirm($(this).attr('data-confirm'))) {
            return false;
        }
        if (!$(this).find('input[name="confirmed"]').length) {
            $(this).append('<input type="hidden" name="confirmed" value="1">');
        }
        return true;
    });

    // 指定推荐页上"按演员补全作品"那一块：有事在进行时（data-refresh="1"）每隔几秒取一次新的（/pick/backfill），
    // 有变化才换上去，进度不用手动刷新也能看到；没事了（data-refresh="0"）就不再取。页面在后台标签页时不取，
    // 回到前台接着取；鼠标正按在这一块的按钮上（焦点在里面）时这一次不换，免得按钮在点下去的时候被换掉
    var BACKFILL_EVERY = 3000;
    var BACKFILL_AFTER_ERROR = 10000;
    var backfillTimer = null;
    var backfillLoading = false;

    function backfillBlock() {
        return document.getElementById('backfill');
    }

    function backfillActive() {
        var block = backfillBlock();
        return block !== null && block.getAttribute('data-refresh') === '1';
    }

    function scheduleBackfill(wait) {
        clearTimeout(backfillTimer);
        backfillTimer = null;
        if (backfillActive() && !document.hidden) {
            backfillTimer = setTimeout(refreshBackfill, wait);
        }
    }

    function refreshBackfill() {
        backfillTimer = null;
        backfillLoading = true;
        var request = new XMLHttpRequest();
        request.onload = function () {
            backfillLoading = false;
            if (request.status !== 200) {
                scheduleBackfill(BACKFILL_AFTER_ERROR);
                return;
            }
            var holder = document.createElement('div');
            holder.innerHTML = request.responseText;
            var fresh = holder.querySelector('#backfill');
            var current = backfillBlock();
            if (fresh && current && fresh.outerHTML !== current.outerHTML
                    && !current.contains(document.activeElement)) {
                current.parentNode.replaceChild(fresh, current);
            }
            scheduleBackfill(BACKFILL_EVERY);
        };
        request.onerror = function () {
            backfillLoading = false;
            scheduleBackfill(BACKFILL_AFTER_ERROR);
        };
        request.open('GET', '/pick/backfill');
        request.send();
    }

    scheduleBackfill(BACKFILL_EVERY);
    $(document).on('visibilitychange', function () {
        if (!document.hidden && backfillTimer === null && !backfillLoading) {
            scheduleBackfill(0);
        }
    });

    // 指定推荐页的输入框：输入时向 /pick/suggest 要联想，列在输入框下面，点一条（或者用上下键选中再按回车）就填进去。
    // 一次可以用逗号隔开写多个名字：只拿最后一个去查，选中时也只换掉最后一个，前面写好的留着。
    // 列表是页面自己画的，不用浏览器自带的 <datalist>：那个下拉只列出文字里含有输入内容的选项，
    // 输入简体时库里用繁体写的名字、逗号后面再写的名字都会被它滤掉
    $('input[data-suggest]').each(function () {
        var input = this;
        var list = $(input).siblings('.pick-suggest-list')[0];
        var kind = input.getAttribute('data-suggest');
        var timer = null;
        // asked 是最近一次去要的网址，loaded 是列表里现在这些联想对应的网址
        var asked = null;
        var loaded = null;
        var composing = false;

        input.setAttribute('role', 'combobox');
        input.setAttribute('aria-autocomplete', 'list');
        input.setAttribute('aria-controls', list.id);
        input.setAttribute('aria-expanded', 'false');
        list.setAttribute('role', 'listbox');

        // 最后一个逗号后面的那个名字：start 是它开始的位置（逗号后面的空白留在前面），text 是去掉首尾空白的文字
        function lastName() {
            var cut = Math.max(input.value.lastIndexOf(','), input.value.lastIndexOf('，')) + 1;
            var rest = input.value.slice(cut);
            return {start: cut + rest.match(/^\s*/)[0].length, text: rest.trim()};
        }

        // 用上下键选到的那一条（row 为空就是哪条都不选）
        function mark(row) {
            $(list).children().removeClass('active').attr('aria-selected', 'false');
            if (row) {
                $(row).addClass('active').attr('aria-selected', 'true');
                input.setAttribute('aria-activedescendant', row.id);
                row.scrollIntoView({block: 'nearest'});
            } else {
                input.removeAttribute('aria-activedescendant');
            }
        }

        function close() {
            mark(null);
            list.hidden = true;
            input.setAttribute('aria-expanded', 'false');
        }

        // 列表里有联想、光标还在这个输入框里、输入法没在组字，才展开
        function open() {
            if (list.firstChild && document.activeElement === input && !composing) {
                list.hidden = false;
                input.setAttribute('aria-expanded', 'true');
            } else {
                close();
            }
        }

        function fill(url, rows) {
            close();
            while (list.firstChild) {
                list.removeChild(list.firstChild);
            }
            rows.forEach(function (row, n) {
                var item = document.createElement('li');
                item.id = list.id + '-' + n;
                item.setAttribute('role', 'option');
                item.setAttribute('data-name', row.value);
                var name = document.createElement('span');
                name.className = 'pick-suggest-name';
                name.textContent = row.value;
                var note = document.createElement('span');
                note.className = 'pick-suggest-note';
                // 女优的别名写明是谁的别名（几位共用时都写出来）；一个名字同时又是别的女优的别名时也写一句
                if (row.alias_of) {
                    note.textContent = row.alias_of.join('、') + ' 的别名 · ' + row.count + ' 部';
                } else {
                    note.textContent = row.type + ' · ' + row.count + ' 部';
                }
                if (row.also_alias_of) {
                    note.textContent += ' · 也是 ' + row.also_alias_of.join('、') + ' 的别名';
                }
                item.appendChild(name);
                item.appendChild(note);
                list.appendChild(item);
            });
            loaded = url;
            open();
        }

        // 换掉的是输入框里现在的最后一个名字：前面的名字在联想出来以后又改过，也按改过的留着
        function choose(row) {
            input.value = input.value.slice(0, lastName().start) + row.getAttribute('data-name');
            close();
        }

        function ask() {
            var url = '/pick/suggest?kind=' + encodeURIComponent(kind) + '&q=' + encodeURIComponent(lastName().text);
            // 要查的文字没变就不再问：联想已经在列表里的，重新展开；还在路上的，等它回来
            if (url === asked) {
                if (url === loaded) {
                    open();
                }
                return;
            }
            asked = url;
            var request = new XMLHttpRequest();
            // 没要到：收起列表，同样的文字下次还去要
            function failed() {
                if (url === asked) {
                    asked = null;
                    fill(null, []);
                }
            }
            request.onload = function () {
                // 输入得快时，先发出去的请求可能后回来，只用最后一次的结果
                if (url !== asked) {
                    return;
                }
                if (request.status === 200) {
                    fill(url, JSON.parse(request.responseText));
                } else {
                    failed();
                }
            };
            request.onerror = failed;
            request.open('GET', url);
            request.send();
        }

        function askSoon() {
            clearTimeout(timer);
            timer = setTimeout(ask, 200);
        }

        $(input).on('input focus', function () {
            // 输入法还在组字时输入框里是拼音之类的中间结果，等上屏了再查
            if (!composing) {
                askSoon();
            }
        });
        $(input).on('compositionstart', function () {
            composing = true;
            clearTimeout(timer);
            close();
        });
        $(input).on('compositionend', function () {
            composing = false;
            askSoon();
        });
        $(input).on('blur', function () {
            clearTimeout(timer);
            close();
        });

        $(input).on('keydown', function (event) {
            // 输入法组字时，上下键和回车是给输入法用的（这时浏览器报的键码是 229）
            if (composing || event.keyCode === 229) {
                return;
            }
            var rows = $(list).children();
            var at = rows.index(rows.filter('.active'));
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                if (list.hidden) {
                    // 收起来以后按向下键：重新展开
                    if (event.key === 'ArrowDown') {
                        event.preventDefault();
                        clearTimeout(timer);
                        ask();
                    }
                    return;
                }
                event.preventDefault();
                // 在"哪条都不选"和各条之间转着走：最后一条再往下回到不选，不选再往上是最后一条
                var next = at + (event.key === 'ArrowDown' ? 1 : -1);
                if (next >= rows.length) {
                    next = -1;
                } else if (next < -1) {
                    next = rows.length - 1;
                }
                mark(next < 0 ? null : rows[next]);
            } else if (event.key === 'Enter') {
                // 选中了一条时回车是填进这一条；没选中时照常提交表单
                if (!list.hidden && at >= 0) {
                    event.preventDefault();
                    choose(rows[at]);
                }
            } else if (event.key === 'Escape') {
                close();
            }
        });

        // 点联想时光标留在输入框里（不然输入框先失去焦点，列表收起来，就点不到了）
        $(list).on('mousedown', function (event) {
            event.preventDefault();
        });
        $(list).on('click', 'li', function () {
            choose(this);
        });

        // 浏览器会把粘贴进单行输入框的换行变成空格，几个名字就连成了一个；先把换行换成逗号。
        // 从表格里复制的一行，格子之间是制表符，同样换成逗号
        $(input).on('paste', function (event) {
            var clipboard = event.originalEvent.clipboardData;
            var text = clipboard ? clipboard.getData('text') : '';
            if (!/[\r\n\t]/.test(text)) {
                return;
            }
            event.preventDefault();
            var names = text.split(/[\r\n\t]+/).map(function (name) {
                return name.trim();
            }).filter(Boolean).join('，');
            var start = input.selectionStart;
            input.value = input.value.slice(0, start) + names + input.value.slice(input.selectionEnd);
            input.selectionStart = input.selectionEnd = start + names.length;
            $(input).trigger('input');
        });
    });
});
