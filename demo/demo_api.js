/* 서버 없이 여는 데모 페이지(GitHub Pages 등)에서 대시보드 서버 API 를 흉내 낸다.
 *
 * 대시보드는 /api/* 로 기록을 주고받는다. 이 스크립트가 그 요청을 가로채 방문자 브라우저
 * (localStorage)에 기록하므로, 버튼이 실제 서버에서처럼 동작하고 방문자끼리 기록이 섞이지 않는다.
 * 처음 상태는 페이지에 심은 데모 기록(#demo-seed)이다. 차단 규칙 변경은 화면을 서버에서 다시
 * 만들어야 해서 안내만 한다. build_demo.py 가 대시보드 스크립트보다 앞에 넣는다.
 */
(function(){
  var seedEl=document.getElementById('demo-seed');
  var seed=JSON.parse(seedEl.textContent);
  var KEY='nwmail.demo.'+seedEl.dataset.version;
  var st;
  try{st=JSON.parse(localStorage.getItem(KEY)||'null')}catch(e){st=null}
  if(!st)st=JSON.parse(JSON.stringify(seed));
  function save(){try{localStorage.setItem(KEY,JSON.stringify(st))}catch(e){}}
  function now(){return new Date().toISOString()}
  function reply(status,body){
    return Promise.resolve(body===null?new Response(null,{status:status}):
      new Response(JSON.stringify(body),{status:status,headers:{'Content-Type':'application/json; charset=utf-8'}}));
  }
  function body(opts){try{return JSON.parse((opts&&opts.body)||'{}')}catch(e){return null}}

  function feedback(data){
    var items=Array.isArray(data.items)?data.items:[data],states={};
    items.slice(0,500).forEach(function(it){
      var id=String(it.task_id||'');
      if(!/^[0-9a-f]{12}$/.test(id))return;
      if(it.label==='open'){delete st.states[id];states[id]=null;return;}
      st.states[id]={label:String(it.label||''),reason:String(it.reason||''),labeled_at:now()};
      states[id]=st.states[id];
    });
    save();return reply(200,{ok:true,states:states});
  }
  function mark(it){
    var group=st[String(it.target||'')+'s'],key=String(it.key||'');
    if(!group||!key)return reply(400,{error:'요청 형식이 올바르지 않습니다'});
    if(it.label==='open'){delete group[key];save();return reply(200,{ok:true,state:null});}
    group[key]={label:String(it.label||''),ref_at:String(it.ref_at||''),labeled_at:now()};
    save();return reply(200,{ok:true,state:group[key]});
  }
  function due(it){
    var id=String(it.task_id||''),d=String(it.due||'');
    if(!d){delete st.dues[id];save();return reply(200,{ok:true,state:null});}
    if(!/^\d{4}-\d{2}-\d{2}$/.test(d))return reply(400,{error:'기한 형식이 올바르지 않습니다'});
    st.dues[id]={due:d,set_at:now()};
    save();return reply(200,{ok:true,state:st.dues[id]});
  }

  var realFetch=window.fetch;
  window.fetch=function(input,opts){
    var url=new URL(typeof input==='string'?input:input.url,location.href);
    var i=url.pathname.indexOf('/api/');
    if(i<0)return realFetch.apply(this,arguments);
    var path=url.pathname.slice(i),method=((opts&&opts.method)||'GET').toUpperCase();
    if(path==='/api/ping')return reply(204,null);
    if(path==='/api/feedback'&&method==='GET')return reply(200,st);
    if(method!=='POST')return reply(404,{error:'없는 주소'});
    var data=body(opts);
    if(!data||typeof data!=='object')return reply(400,{error:'요청 형식이 올바르지 않습니다'});
    if(path==='/api/feedback')return feedback(data);
    if(path==='/api/mark')return mark(data);
    if(path==='/api/due')return due(data);
    if(path==='/api/filters')return reply(400,{error:'온라인 데모에서는 차단 규칙을 바꾸지 않아요. '+
      '로컬 데모 서버(python demo/serve_demo.py)에서는 실제로 바뀝니다.'});
    return reply(404,{error:'없는 주소'});
  };

  document.addEventListener('click',function(e){
    if(!e.target.closest||!e.target.closest('#demo-reset'))return;
    e.preventDefault();
    try{localStorage.removeItem(KEY)}catch(err){}
    location.reload();
  });
})();
