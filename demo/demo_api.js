/* 서버 없이 여는 데모 페이지(GitHub Pages 등)에서 대시보드 서버 API 를 흉내 낸다.
 *
 * 대시보드는 /api/* 로 기록을 주고받는다. 이 스크립트가 그 요청을 가로채 방문자 브라우저
 * (localStorage)에 기록하므로, 버튼이 실제 서버에서처럼 동작하고 방문자끼리 기록이 섞이지 않는다.
 * 처음 상태와 허용 값은 운영 FeedbackStore 로 만든 #demo-seed/#demo-contract 에서 읽는다.
 * 차단 규칙은 전체 대시보드를 다시 계산해야 하므로 로컬 데모 서버에서 제공한다.
 */
(function(){
  var seedEl=document.getElementById('demo-seed');
  var seed=JSON.parse(seedEl.textContent);
  var contract=JSON.parse(document.getElementById('demo-contract').textContent);
  var LEGACY_KEY='nwmail.demo.'+seedEl.dataset.version;
  // 빌드 내용이 바뀌어도 방문자가 눌렀던 기록을 유지한다.
  var KEY='mailwork.demo.state.v1',st;
  try{st=JSON.parse(localStorage.getItem(KEY)||localStorage.getItem(LEGACY_KEY)||'null')}catch(e){st=null}
  function object(v){return !!v&&typeof v==='object'&&!Array.isArray(v);}
  if(!object(st))st=JSON.parse(JSON.stringify(seed));
  ['states','dues','colors','threads','projects'].forEach(function(k){
    if(!object(st[k]))st[k]=JSON.parse(JSON.stringify(seed[k]||{}));
  });
  function save(){try{localStorage.setItem(KEY,JSON.stringify(st))}catch(e){}}
  function now(){return new Date().toISOString()}
  function reply(status,body){
    return Promise.resolve(body===null?new Response(null,{status:status}):
      new Response(JSON.stringify(body),{status:status,headers:{'Content-Type':'application/json; charset=utf-8'}}));
  }
  function body(opts){try{return JSON.parse((opts&&opts.body)||'{}')}catch(e){return null}}
  function bad(message){return reply(400,{error:message||'요청 형식이 올바르지 않습니다'});}
  function has(o,k){return Object.prototype.hasOwnProperty.call(o,k);}
  function keyOf(value){var k=String(value||'').trim();
    return k&&k.length<=500&&['__proto__','constructor','prototype'].indexOf(k)<0?k:'';}
  function validId(id){return /^[0-9a-f]{12}$/.test(id);}
  function validDay(d){
    if(!/^\d{4}-\d{2}-\d{2}$/.test(d))return false;
    var parsed=new Date(d+'T00:00:00Z');
    return !isNaN(parsed.getTime())&&parsed.toISOString().slice(0,10)===d;
  }
  function labeledAt(on){
    // 운영 서버와 같이 과거 날짜를 고르면 한국 시간 정오로 기록한다.
    var value=String(on||''),stamp=validDay(value)?new Date(value+'T12:00:00+09:00'):null;
    return stamp&&stamp<=new Date()?stamp.toISOString():now();
  }

  function feedback(data){
    var items=Array.isArray(data.items)?data.items:[data],states={};
    var invalid=items.slice(0,500).some(function(it){
      if(!object(it)||!validId(String(it.task_id||'')))return true;
      var label=String(it.label||''),reason=String(it.reason||'');
      return label!=='open'&&!has(contract.labels,label)||
        reason&&((contract.reasons[label]||[]).indexOf(reason)<0);
    });
    if(invalid)return bad('할 일 ID · 표시 · 사유가 올바르지 않습니다');
    items.slice(0,500).forEach(function(it){
      var id=String(it.task_id||'');
      if(it.label==='open'){delete st.states[id];states[id]=null;return;}
      st.states[id]={label:String(it.label||''),reason:String(it.reason||''),labeled_at:labeledAt(it.on)};
      states[id]=st.states[id];
    });
    save();return reply(200,{ok:true,states:states});
  }
  function mark(it){
    var target=String(it.target||''),key=keyOf(it.key),label=String(it.label||''),ref=String(it.ref_at||'').trim();
    if(!has(contract.marks,target)||!key)return bad();
    if(label!=='open'&&!has(contract.marks[target],label))return bad('표시가 올바르지 않습니다');
    if(ref&&isNaN(Date.parse(ref)))return bad('기준 시각 형식이 올바르지 않습니다');
    var group=st[target+'s'];
    if(it.label==='open'){delete group[key];save();return reply(200,{ok:true,state:null});}
    group[key]={label:label,ref_at:ref,labeled_at:now()};
    save();return reply(200,{ok:true,state:group[key]});
  }
  function due(it){
    var id=String(it.task_id||''),d=String(it.due||'').trim();
    if(!validId(id))return bad('할 일 ID 가 올바르지 않습니다');
    if(!d){delete st.dues[id];save();return reply(200,{ok:true,state:null});}
    if(!validDay(d))return bad('기한 형식이 올바르지 않습니다');
    if(Math.abs(+d.slice(0,4)-new Date().getUTCFullYear())>3)return bad('기한은 올해 앞뒤 3년 안에서만 정할 수 있습니다');
    st.dues[id]={due:d,set_at:now()};
    save();return reply(200,{ok:true,state:st.dues[id]});
  }

  function color(it){
    var name=keyOf(it.project),c=String(it.color||'').trim().toLowerCase();
    if(!name)return bad();
    if(c&&!/^(s[1-9]|#[0-9a-f]{6})$/.test(c))return reply(400,{error:'색 형식이 올바르지 않습니다'});
    if(!c)delete st.colors[name];else st.colors[name]={color:c};
    save();return reply(200,{ok:true,color:{color:c}});   /* 밝기 맞춤은 페이지가 직접 한다 */
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
    if(!object(data))return bad();
    if(path==='/api/feedback')return feedback(data);
    if(path==='/api/mark')return mark(data);
    if(path==='/api/due')return due(data);
    if(path==='/api/color')return color(data);
    if(path==='/api/filters')return reply(501,{error:'차단 규칙·보호 도메인 변경은 로컬 데모 서버에서 체험할 수 있습니다. '+
      'python demo/serve_demo.py 로 실행하면 운영 코드로 전체 대시보드를 다시 계산합니다.'});
    return reply(404,{error:'없는 주소'});
  };

  document.addEventListener('click',function(e){
    if(!e.target.closest||!e.target.closest('#demo-reset'))return;
    e.preventDefault();
    try{localStorage.removeItem(KEY);localStorage.removeItem(LEGACY_KEY)}catch(err){}
    location.reload();
  });
})();
