const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = name => fs.readFileSync(path.join(__dirname, '../src', name), 'utf8');
const index = source('index.html');

test('English likelihoods preserve weighted fate selection and legacy saves', () => {
    const start = index.indexOf('const fatePick =');
    const end = index.indexOf('// ── Tree Layout', start);
    for (const labels of [[' Very High (likely)', 'Very low (unlikely)'], ['\u6781\u9ad8', '\u6781\u4f4e']]) {
        let selected;
        const context = {optionLikelihoods:{value:{1:labels[0],2:labels[1]}},
            currentNode:{value:{options:[{next_node_id:1},{next_node_id:2}]}},
            fateSpinning:{value:false}, fateHighlightIdx:{value:-1},
            Math:Object.assign(Object.create(Math),{random:()=>0.75}),
            setTimeout:callback=>callback(), jumpToNode:id=>{selected=id;}};
        vm.runInNewContext(index.slice(start,end)+'\nfatePick();',context);
        assert.equal(selected,1);
    }
});

test('multiword English location aliases stay intact', () => {
    const handler = index.match(/id="map-room-aliases"[^>]*@change="([^"]+)"/)[1];
    const context = {mapSelectedRoom:{},$event:{target:{value:'West Wing, Old Hall; North Tower'}},mapSaveRoom(){}};
    vm.runInNewContext(handler,context);
    assert.deepEqual(Array.from(context.mapSelectedRoom.aliases),['West Wing','Old Hall','North Tower']);
});

test('phone previews remove English and legacy private-chat markers', () => {
    const expression = source('phone.html').match(/lastMsg: (\(\(\) => \{[^\n]+\}\)\(\)),/)[1];
    for (const tag of ['[PM]','[\u79c1\u804a]']) {
        const preview = vm.runInNewContext(expression,{sd:{memory:[tag+' Please inspect the console.']}});
        assert.equal(preview,'Please inspect the console.');
    }
});
