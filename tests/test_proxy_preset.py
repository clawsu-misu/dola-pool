"""面板 1024Proxy 预设按钮：填的模板与默认值符合官方账密格式与本项目出口要求。"""
import shutil
import subprocess
from pathlib import Path


def run_preset(kind):
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node required")
    html = (Path(__file__).resolve().parents[1] / "web/index.html").read_text(encoding="utf-8")
    start = html.index("function preset1024(")
    end = html.index("let proxyProbeCache", start)
    code = html[start:end]
    script = r'''
const assert=require('assert');
const kind=process.argv[1];
const nodes={
 '#f_pmode':{value:'static'},
 '#f_ptpl':{value:''},
 '#f_pttl':{value:''},
 '#f_pextint':{value:''},
 '#f_pexturl':{value:''},
};
let focused='',toasts=[];
const radios={};
const q=s=>{
  const m=/\[value=(\w+)\]/.exec(s);
  if(m)return radios[m[1]]||(radios[m[1]]={checked:false});
  return nodes[s];
};
nodes['#f_ptpl'].focus=()=>{focused='tpl';};
nodes['#f_pexturl'].focus=()=>{focused='url';};
const toast=m=>toasts.push(m);
const onProxyModeChange=()=>{},onProxyKindChange=()=>{};
''' + code + r'''
preset1024(kind);
assert.equal(nodes['#f_pmode'].value,'extract');
assert.equal(radios[kind].checked,true);
if(kind==='tunnel'){
  assert.equal(nodes['#f_ptpl'].value,'{user}-region-JP-sid-{session}-t-120');
  assert.equal(nodes['#f_pttl'].value,7200);
  assert.equal(focused,'tpl');
}else{
  assert.equal(nodes['#f_pextint'].value,60);
  assert.equal(focused,'url');
}
assert.equal(toasts.length,1);
assert.ok(toasts[0].includes('1024'));
'''
    subprocess.run([node, "-e", script, kind], check=True, capture_output=True, timeout=15)


def test_tunnel_preset_fills_official_template():
    run_preset("tunnel")


def test_api_extract_preset_sets_interval_and_focuses_url():
    run_preset("url")
