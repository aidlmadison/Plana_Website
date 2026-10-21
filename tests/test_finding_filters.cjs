const assert=require('node:assert/strict');
const test=require('node:test');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8');
const functions=source.slice(source.indexOf('function instructorReviewed('),source.indexOf('function renderFindings('));
const findings=[
  {id:'open',status:'open',reviewer:null,page:1},
  {id:'pending',status:'pending',reviewer:null,page:2},
  {id:'accepted',status:'accepted',reviewer:'instructor1',page:2},
  {id:'rejected',status:'rejected',reviewer:'instructor2',page:3},
  {id:'canceled',status:'canceled',reviewer:null,page:1},
  {id:'unreviewed',status:'accepted',reviewer:null,page:1},
];
function visible(role,mode){
  const context={state:{user:{role},submission:{findings},page:1},$:()=>({value:mode})};
  vm.createContext(context);
  return [...vm.runInContext(functions+'\nvisibleFindings().map(item=>item.id)',context)];
}
test('student review filter includes only completed instructor decisions across pages',()=>{
  assert.deepEqual(visible('student','pending'),['accepted','rejected']);
});
test('instructor review queue continues to show pending requests',()=>{
  assert.deepEqual(visible('instructor','pending'),['pending']);
});
test('page and all filters retain their existing behavior',()=>{
  assert.deepEqual(visible('student','page'),['open','canceled','unreviewed']);
  assert.deepEqual(visible('student','all'),findings.map(item=>item.id));
});
