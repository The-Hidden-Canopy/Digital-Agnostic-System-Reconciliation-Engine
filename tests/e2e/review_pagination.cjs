const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../src/verifier_lab/web/app.js'), 'utf8');
const code = source.slice(source.indexOf('async function viewReview('), source.indexOf('function renderBulk()'));
const h = (tag, attrs, ...kids) => ({tag, attrs: attrs || {}, kids: kids.flat(Infinity).filter(Boolean)});
let tree, total = 401, requests = [], responses = [];
const ctx = vm.createContext({URLSearchParams, REVIEW_PAGE_SIZE: 200, reviewOffset: 0,
  state: {filters: {source:'', status:'PENDING', q:''}, candSel:new Set()},
  begin: () => () => true, setNav(){}, h, main:{}, put(_main,...nodes){tree=nodes},
  renderBulk(){}, candidateDetail(){}, fmtVal: () => '', badge: h,
  api: async (_method, endpoint) => {
    if (endpoint === 'sources') return {items:[]};
    const q = new URLSearchParams(endpoint.split('?')[1]); requests.push(q);
    const offset = Number(q.get('offset')), limit = Number(q.get('limit'));
    if (responses.length) return responses.shift();
    return {total, offset, items: Array.from({length: Math.max(0, Math.min(limit,total-offset))}, (_,i)=>({id:String(offset+i),status:'PENDING',fact_key:`key${offset+i}`,source_name:'synthetic',predicate:{arguments:{expected:{}}}}))};
  }});
vm.runInContext(code, ctx);
function all(nodes) {return nodes.flatMap(n=>n && typeof n==='object'?[n,...all(n.kids || [])]:[])}
const find = (text) => all(tree).find(n => n.attrs.text===text);
const field = (id) => all(tree).find(n => n.attrs.id===id);
const show = () => ctx.viewReview(null,new URLSearchParams());
const tick = () => new Promise(resolve=>setImmediate(resolve));
(async()=>{
  await show(); assert(find('Showing 1-200 of 401 (denominator: current extractions only)')); assert(find('Previous candidates').attrs.disabled);
  await find('Next candidates').attrs.onclick(); assert(find('Showing 201-400 of 401 (denominator: current extractions only)'));
  await find('Next candidates').attrs.onclick(); assert(find('Showing 401-401 of 401 (denominator: current extractions only)')); assert(find('Next candidates').attrs.disabled);
  total=400; await show(); assert(find('Showing 201-400 of 400 (denominator: current extractions only)')); assert.equal(requests.at(-1).get('offset'),'200');
  await find('Previous candidates').attrs.onclick(); assert(find('Showing 1-200 of 400 (denominator: current extractions only)'));
  for(const id of ['f-src','f-st','f-q']) {await find('Next candidates').attrs.onclick(); field(id).attrs.onchange({target:{value:'changed'}}); await tick(); assert.equal(requests.at(-1).get('offset'),'0');}
  await find('Next candidates').attrs.onclick(); await ctx.viewReview(null,new URLSearchParams('source=other')); assert.equal(requests.at(-1).get('offset'),'0');
  total=0; await show(); assert(find('Showing 0-0 of 0 (denominator: current extractions only)')); assert(!find('Next candidates'));
  total=401; await show(); await find('Next candidates').attrs.onclick();
  // Server reports enough candidates for this offset but no items; no loop.
  let before=requests.length; responses=[{total:401,offset:200,items:[]}]; await show();
  assert.equal(requests.length-before,1);
  assert(find('No candidates returned on this page (401 reported; refresh or change pages).'));
  assert(!find('Previous candidates').attrs.disabled);
  // Two successive shrinking totals only trigger one lower-offset requery.
  await find('Next candidates').attrs.onclick();
  before=requests.length; responses=[{total:222,offset:400,items:[]},{total:1,offset:200,items:[]}]; await show();
  assert.equal(requests.length-before,2); assert.equal(requests.at(-1).get('offset'),'200');
  assert(find('No candidates returned on this page (1 reported; refresh or change pages).'));
  assert(!find('Previous candidates').attrs.disabled);
  // Malformed numeric metadata and offset mismatch do not manufacture a denominator.
  for (const bad of [
    {total:NaN,offset:200,items:[]},{total:Infinity,offset:200,items:[]},
    {total:-1,offset:200,items:[]},{total:201.5,offset:200,items:[]},
    {total:401,offset:200.5,items:[]},{total:401,offset:-1,items:[]},
    {total:401,offset:0,items:[]},{total:401,offset:Infinity,items:[]}
  ]) {
    before=requests.length; responses=[bad]; await show(); assert.equal(requests.length-before,1);
    assert(find('Candidate page unavailable (invalid pagination response; refresh or change filters).'));
    assert(find('Next candidates').attrs.disabled); assert(!find('Previous candidates').attrs.disabled);
  }
  console.log('Review pagination: 19 synthetic checks passed');
})().catch(error=>{console.error(error);process.exitCode=1});
