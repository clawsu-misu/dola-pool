"""面板「1024Proxy」专用页签：独立表单、链接解析、提交内容，以及通用代理表单里不再有 1024 的东西。

用 node 跑 web/index.html 里真实的面板函数（极简 DOM + 桩 api），不访问网络；没装 node 时跳过。
"""
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = Path(__file__).resolve().parents[1] / "web/index.html"

NODE = r'''
const fs=require('fs'),assert=require('assert');
const html=fs.readFileSync(process.argv[1],'utf8'),scenario=process.argv[2];
const start=html.indexOf('function proxyAddTabs(on){'),end=html.indexOf('/** 快捷添加：',start);
assert.ok(start>0&&end>start,'找不到 1024Proxy 面板代码');
const code=html.slice(start,end);
const ps=html.indexOf('function proxyProtocolOptions(sel){'),pe=html.indexOf('function proxyModeOptions(sel){',ps);
assert.ok(ps>0&&pe>ps);
const es=html.indexOf('function esc(s){'),ee=html.indexOf('\n',es);
assert.ok(es>0&&ee>es);
const gs=html.indexOf('function proxyDialog(p){'),ge=html.indexOf('function openAddProxy()',gs);
const genericDialog=html.slice(gs,ge);
const LINK='https://white.1024proxy.com/white/api?region=Rand&num=1&time=10&format=1&type=txt';

(async()=>{
  const cap={calls:[],alerts:[],verified:[],copied:[],html:'',response:null};
  const nodes={};
  const q=s=>nodes[s]||(nodes[s]={value:'',style:{},dataset:{},textContent:''});
  const api=async(url,opt)=>{opt=opt||{};if(cap.response instanceof Error)throw cap.response;cap.calls.push([url,opt.method,opt.body]);return cap.response;};
  let alert=m=>cap.alerts.push(String(m));
  let copy=t=>cap.copied.push(t),toast=m=>cap.alerts.push(String(m));
  let hide=()=>{},loadProxies=()=>{},proxyProbeCache={};
  const dialogText=(strings,...values)=>({i18nParts:strings,i18nValues:values,
    toString(){return strings.reduce((o,p,i)=>o+p+(i<values.length?String(values[i]):''),'');}});
  const showDlg=h=>{cap.html=h;};
  eval(html.slice(ps,pe)+html.slice(es,ee)+code);                        // 真实的协议下拉 / 转义函数
  if(scenario!=='verify')verify1024=p=>cap.verified.push(p.id);
  const fill=(id,v)=>{q(id).value=v;};
  const kindOf=p=>q('input[name=f_1024kind]:checked').value=p;

  if(scenario==='tabs'){
    const tabs=proxyAddTabs('1024');
    assert.ok(tabs.includes('proxy1024Dialog()'));
    assert.equal((tabs.match(/ptab on/g)||[]).length,1);
    assert.ok(/ptab on" onclick="proxy1024Dialog\(\)/.test(tabs));
    assert.ok(!/ptab on" onclick="proxy1024Dialog/.test(proxyAddTabs('std')));
  }else if(scenario==='parse'){
    assert.deepEqual(parse1024Link(LINK),{region:'RAND',num:1,time:10});
    const u=new URL(LINK);u.searchParams.set('region','kr');u.searchParams.set('num','5');u.searchParams.set('time','15');
    assert.deepEqual(parse1024Link(u.href),{region:'KR',num:5,time:15});
    u.searchParams.delete('num');u.searchParams.delete('time');
    assert.deepEqual(parse1024Link(u.href),{region:'KR',num:1,time:10});      // 缺参数给默认值
    assert.equal(parse1024Link(LINK.replace('white.1024proxy.com','api.other-vendor.com')),null);
    assert.equal(parse1024Link('随便贴的'),null);
    assert.equal(parse1024Link(''),null);
  }else if(scenario==='badge'){
    const badge=p=>proxyTypeBadge(p).replace(/<[^>]+>/g,'');
    assert.equal(badge({provider:'1024proxy',mode:'dynamic'}),'1024Proxy 隧道');
    assert.equal(badge({provider:'1024proxy',mode:'extract'}),'1024Proxy API');
    assert.equal(badge({mode:'extract',extract_url:'https://a'}),'提取链接');
    assert.equal(badge({mode:'extract'}),'粘贴节点');
    assert.equal(badge({mode:'dynamic',is_dynamic:true}),'隧道');
    assert.equal(badge({mode:'static'}),'静态');
  }else if(scenario==='kind'){
    kindOf('api');on1024KindChange();
    assert.equal(q('#p1024api').style.display,'block');assert.equal(q('#p1024tunnel').style.display,'none');
    kindOf('tunnel');on1024KindChange();
    assert.equal(q('#p1024tunnel').style.display,'block');assert.equal(q('#p1024api').style.display,'none');
  }else if(scenario==='link'){
    fill('#f_1024link',LINK);
    q('#f_1024link').dataset.fill='1';                                   // 新建：按链接填默认值
    on1024LinkChange();
    assert.deepEqual([q('#f_1024num').value,q('#f_1024time').value,q('#f_1024region').value],[1,10,'JP']);
    assert.equal(q('#f_1024linkinfo').textContent,'链接里的地区：RAND · 数量：1 · 粘性：10 分钟（不是日韩，实际按上面的「出口地区」提取）');
    const u=new URL(LINK);u.searchParams.set('region','KR');u.searchParams.set('num','4');
    fill('#f_1024link',u.href);on1024LinkChange();
    assert.deepEqual([q('#f_1024num').value,q('#f_1024region').value],[4,'KR']);
    assert.equal(q('#f_1024linkinfo').textContent,'链接里的地区：KR · 数量：4 · 粘性：10 分钟');
    fill('#f_1024num','9');q('#f_1024link').dataset.fill='0';            // 编辑：不覆盖已保存的设置
    fill('#f_1024link',LINK);on1024LinkChange();
    assert.equal(q('#f_1024num').value,'9');assert.equal(q('#f_1024region').value,'KR');
    fill('#f_1024link','https://api.other-vendor.com/get');on1024LinkChange();
    assert.equal(q('#f_1024linkinfo').textContent,'');
  }else if(scenario==='dialog'){
    proxy1024Dialog(null);
    let h=cap.html;
    assert.ok(h.includes('<h2>添加 1024Proxy</h2>')&&h.includes('⚡ 1024Proxy</button>'));
    for(const id of ['f_1024name','f_1024protocol','f_1024region','f_1024gateway','f_1024user','f_ppass',
                     'f_1024ttl','f_1024link','f_1024num','f_1024time','f_1024remark'])
      assert.ok(h.includes('id="'+id+'"'),'缺少 '+id);
    assert.ok(h.includes('id="p1024tunnel" style="display:block"')&&h.includes('id="p1024api" style="display:none"'));
    assert.ok(h.includes('data-fill="1"'));
    assert.equal((h.match(/name="f_1024kind"[^>]*disabled/g)||[]).length,0);
    assert.ok(h.includes('value="JP" selected')&&h.includes('value="120"'));
    assert.ok(!h.includes('f_ptpl'));                                    // 没有用户名模板输入框
    const p={id:'px1',name:'n<&>',mode:'extract',protocol:'socks5',host:'',port:0,extract_url:LINK,
             username:'',remark:'r',has_password:true,provider_settings:{region:'KR',num:3,time:15}};
    proxy1024Dialog(p);h=cap.html;
    assert.ok(h.includes('<h2>编辑 1024Proxy</h2>')&&!h.includes('ptabs'));
    assert.ok(h.includes('id="p1024api" style="display:block"')&&h.includes('data-fill="0"'));
    assert.equal((h.match(/name="f_1024kind"[^>]*disabled/g)||[]).length,2);
    assert.ok(h.includes('value="KR" selected')&&h.includes('value="3"')&&h.includes('value="15"'));
    assert.ok(h.includes('value="socks5" selected')&&h.includes('value="n&lt;&amp;&gt;"'));
    assert.ok(h.includes('原密码已保存，留空保持不变'));
  }else if(scenario==='submit'){
    cap.response={created:{id:'px_new',mode:'dynamic'}};
    fill('#f_1024name','   ');fill('#f_1024protocol','http');fill('#f_1024region','JP');
    await submitProxy1024('');
    assert.deepEqual(cap.alerts,['请填写名称']);assert.equal(cap.calls.length,0);   // 名称只填了空格

    fill('#f_1024name',' 1024 日本 ');fill('#f_1024remark','备注');
    kindOf('tunnel');fill('#f_1024gateway',' us.1024proxy.io:3000 ');fill('#f_1024user','acct');
    fill('#f_ppass','p@ss');q('#f_ppass').dataset.dirty='1';fill('#f_1024ttl','90');
    await submitProxy1024('');
    assert.deepEqual(cap.calls,[['/api/admin/proxies/1024','POST',{kind:'tunnel',name:'1024 日本',protocol:'http',
      region:'JP',remark:'备注',gateway:'us.1024proxy.io:3000',username:'acct',ttl:90,password:'p@ss'}]]);
    assert.deepEqual(cap.verified,['px_new']);assert.equal(cap.alerts.length,1);  // 新建后自检

    cap.calls=[];kindOf('api');fill('#f_1024link',LINK);fill('#f_1024num','4');fill('#f_1024time','15');
    await submitProxy1024('');
    assert.deepEqual(cap.calls,[['/api/admin/proxies/1024','POST',{kind:'api',name:'1024 日本',protocol:'http',
      region:'JP',remark:'备注',link:LINK,num:4,time:15}]]);
    assert.deepEqual(cap.verified,['px_new','px_new']);                       // 每次新建后都自检

    // 编辑：留空密码 = 沿用原密码
    cap.calls=[];cap.response={updated:{id:'px1'}};
    fill('#f_ppass','');q('#f_ppass').dataset.dirty='0';
    await submitProxy1024('px1');
    assert.equal(cap.calls[0][0],'/api/admin/proxies/px1/1024');assert.equal(cap.calls[0][1],'PATCH');
    assert.ok(!('password' in cap.calls[0][2])&&cap.calls[0][2].kind==='api'&&cap.verified.length===2);  // 编辑不自检
    cap.calls=[];kindOf('tunnel');fill('#f_1024gateway','hk.1024proxy.io:3000');fill('#f_1024user','acct');
    fill('#f_1024ttl','120');fill('#f_ppass','new');q('#f_ppass').dataset.dirty='1';
    await submitProxy1024('px1');
    assert.equal(cap.calls[0][2].password,'new');

    cap.calls=[];cap.response=new Error('请先登录');                      // 服务端拒绝时原样提示
    await submitProxy1024('px1');
    assert.equal(cap.calls.length,0);assert.ok(cap.alerts[1].includes('请先登录'));
  }else if(scenario==='verify'){
    const probe={ok:true,ip:'1.2.3.4',country:'Japan',country_code:'JP',isp:'ISP'};
    cap.response=probe;await verify1024({id:'px1',mode:'dynamic'});
    assert.ok(cap.alerts[0].includes('出口 IP：1.2.3.4')&&!cap.alerts[0].includes('不是日本/韩国'));
    assert.equal(proxyProbeCache.px1,'1.2.3.4 · Japan · ISP');
    cap.response={...probe,country_code:'US'};await verify1024({id:'px2',mode:'dynamic'});
    assert.ok(cap.alerts[1].includes('不是日本/韩国出口，dola 会拒绝'));
    cap.response={ok:false,error:'407 Proxy Authentication Required'};await verify1024({id:'px3',mode:'dynamic'});
    assert.ok(cap.alerts[2].includes('探测出口失败：407 Proxy Authentication Required'));
    assert.deepEqual(cap.calls.map(c=>c[0].split('/').pop()),['probe','probe','probe']);
    cap.calls=[];cap.response={ok:true,added:3};await verify1024({id:'px4',mode:'extract'});
    assert.ok(cap.alerts[3].includes('3 个地址'));
    cap.response={ok:false,error:'1024Proxy 没有返回可用节点（IP not in whitelist）'};await verify1024({id:'px5',mode:'extract'});
    assert.ok(cap.alerts[4].includes('首次提取失败')&&cap.alerts[4].includes('IP not in whitelist'));
    assert.ok(cap.calls.every(c=>c[0].endsWith('/refresh-nodes')&&c[1]==='POST'));
  }else if(scenario==='ip'){
    cap.response={ok:true,ip:'203.0.113.8'};
    proxy1024Dialog(null);
    assert.ok(cap.html.includes('https://api.1024proxy.com/add/share/etwsrptz8'));
    assert.ok(cap.html.includes('没有 IP？邀请好友获取')&&cap.html.includes('target="_blank"'));
    assert.ok(cap.html.includes('id="p1024ip"')&&cap.html.includes('translate="no"'));
    assert.ok(cap.html.includes('copy1024Ip()')&&cap.html.includes('detect1024Ip(true)'));
    await detect1024Ip();                                             // 打开页签时已经查过一次，这里再确认结果
    assert.equal(q('#p1024ip').textContent,'203.0.113.8');
    assert.equal(q('#p1024ipState').textContent,'本机公网 IP（复制后粘贴到 1024 控制台白名单）：');
    assert.ok(cap.calls.some(c=>c[0]==='/api/admin/proxies/1024/public-ip'));
    cap.calls=[];cap.response={ok:false,error:'超时'};
    await detect1024Ip(true);
    assert.equal(q('#p1024ip').textContent,'');
    assert.equal(q('#p1024ipState').textContent,'检测不到本机公网 IP');
    assert.equal(q('#p1024ipErr').textContent,'超时');
    assert.equal(cap.calls[0][0],'/api/admin/proxies/1024/public-ip?refresh=1');
    q('#p1024ip').textContent='203.0.113.8';
    copy1024Ip();
    assert.deepEqual(cap.copied,['203.0.113.8']);
    assert.ok(cap.alerts.at(-1).includes('已复制'));
    q('#p1024ip').textContent='  ';
    copy1024Ip();
    assert.ok(String(cap.alerts.at(-1)).includes('还没有检测到公网 IP'));
  }else if(scenario==='generic'){
    assert.ok(!html.includes('preset1024'),'通用代理表单不应再有 1024Proxy 预设');
    assert.equal((genericDialog.match(/1024Proxy/g)||[]).length,1);      // 只剩「转到专用表单」那一行
    assert.ok(genericDialog.includes("if(p&&p.provider==='1024proxy')return proxy1024Dialog(p);"));
    assert.ok(!html.includes('1024Proxy 的 API 模式要先在控制台'));
  }else{
    throw new Error('unknown scenario '+scenario);
  }
})().catch(e=>{console.error(e);process.exit(1);});
'''

SCENARIOS = ["tabs", "parse", "badge", "kind", "link", "dialog", "submit", "verify", "ip", "generic"]


def _run(scenario):
    node = shutil.which("node")
    if not node:
        pytest.skip("node required")
    subprocess.run([node, "-e", NODE, str(HTML), scenario],
                   check=True, capture_output=True, timeout=30)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_proxy_1024_panel(scenario):
    _run(scenario)
