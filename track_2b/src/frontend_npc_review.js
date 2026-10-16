/* A single accessible review flow shared by main/squad generation and direct NPC creation. */
window.WorldNPCReview = (() => {
    let opened = false;
    const element = (tag, text, className) => {
        const el = document.createElement(tag);
        if (text !== undefined) el.textContent = text;
        if (className) el.className = className;
        return el;
    };
    function dialog(title) {
        if (opened) throw new Error('请先完成当前角色审核。');
        opened = true;
        const previous = document.activeElement;
        const panel = element('dialog', undefined, 'npc-review-dialog');
        panel.setAttribute('role', 'dialog');
        panel.setAttribute('aria-modal', 'true');
        panel.setAttribute('aria-labelledby', 'npc-review-title');
        // Native top-layer focus/cancel must not activate an underlying Vue dialog's handlers.
        panel.addEventListener('keydown', event => {
            if (event.key === 'Tab' || event.key === 'Escape') event.stopPropagation();
        });
        const heading = element('h2', title); heading.id = 'npc-review-title';
        panel.append(heading);
        document.body.append(panel);
        panel.showModal();
        return {panel, close() {panel.close(); panel.remove(); opened = false; previous?.focus();}};
    }
    function resolve(review) {
        return new Promise(resolve => {
            const maps=review.entries.some(e=>e.kind==='map');
            const {panel, close} = dialog(maps ? (review.entries.some(e=>e.kind!=='map') ? '核对世界变化' : '核对地图变化') : '核对登场人物');
            const intro = element('p', review.message);
            const list = element('div', undefined, 'npc-review-list');
            const error = element('p', '', 'npc-review-error'); error.setAttribute('role', 'alert');
            const actions = element('div', undefined, 'npc-review-actions');
            const cancel = element('button', '取消本次生成', 'btn btn-ghost');
            const submit = element('button', '应用选择并继续', 'btn btn-accent');
            actions.append(cancel, submit); panel.append(intro, list, error, actions);
            const controls = new Map();
            let busy = false, key = WorldAPI.operationKey(), previousChoices = '';
            const finish = result => {close(); resolve(result);};
            const stop = event => {event?.preventDefault(); if (!busy) finish({status:'cancelled'});};
            panel.addEventListener('cancel', stop); cancel.onclick = stop;
            function render(data) {
                review = data; intro.textContent = data.message; list.replaceChildren(); controls.clear();
                for (const item of data.entries) {
                    const section = element('fieldset');
                    section.append(element('legend', `分支 ${item.branch + 1} · ${item.name}`));
                    const isMap=item.kind==='map';
                    section.append(element('p', item.description || (isMap ? '尚无区域描述' : '尚无完整档案')));
                    section.append(element('p', item.reason || (isMap ? '请核对地点身份与必要性。' : '请核对是否需要登记此人物。'), 'npc-review-reason'));
                    const select = element('select'); select.required = true;
                    select.setAttribute('aria-label', `${item.name}的处理方式`);
                    const add = (value, label) => {const opt = element('option', label); opt.value = value; select.append(opt);};
                    add('', '请选择处理方式');
                    for (const candidate of item.candidates || []) {
                        if(isMap && !Object.hasOwn(item.options,candidate.choice)) continue;
                        add(isMap ? candidate.choice : 'reuse:' + candidate.entity_uid,
                            `${isMap && candidate.choice.startsWith('reveal:') ? '揭示已有区域' : '复用'}：${candidate.name} · ${candidate.location || '位置未记录'}${isMap ? (candidate.connection ? ' · 有登记通道' : ' · 无直接通道') : ''}`);
                    }
                    if(!isMap) for (const other of data.entries)
                        if (other.kind!=='map' && other.branch === item.branch && other.key < item.key)
                            add('same:' + other.ref, `同一人物：本分支的${other.name}`);
                    if(!isMap || Object.hasOwn(item.options,'create')) add('create',isMap ? '明确登记一个独立新区域' : '明确作为不同的新人物登记');
                    add('omit',isMap ? '取消这项地图变化' : '取消新增／仅作为背景描写');
                    if (item.decision !== 'unknown') select.value = item.decision;
                    section.append(select);
                    if(isMap && item.mode==='new') {
                        const arrival=element('select'); arrival.required=true;
                        arrival.setAttribute('aria-label',`${item.name}是否进入`);
                        for(const [value,label] of [['','请选择队伍最终位置'],['stay','仅发现区域，队伍保持原位'],['enter','本分支实际进入目标区域']]) {
                            const option=element('option',label); option.value=value; arrival.append(option);
                        }
                        if(item.arrival!=='unknown') arrival.value=item.arrival;
                        select.addEventListener('change',()=>{if(select.value==='omit') arrival.value='stay';});
                        section.append(arrival); controls.set(item.key+':arrival',arrival);
                    }
                    const details = element('details'); details.append(element('summary',isMap ? '查看已有地点资料' : '查看已有身份资料'));
                    for (const candidate of item.candidates || []) {
                        details.append(element('strong', candidate.name));
                        details.append(element('p', [candidate.aliases?.join('、'), candidate.description].filter(Boolean).join(' · ')));
                    }
                    if (item.candidates?.length) section.append(details);
                    list.append(section); controls.set(item.key, select);
                }
            }
            render(review);
            submit.onclick = async () => {
                if (busy) return;
                const choices = {};
                for (const [id, select] of controls) {
                    if (!select.value) {select.reportValidity(); return;}
                    choices[id] = select.value;
                }
                const signature = JSON.stringify(choices);
                if (previousChoices && signature !== previousChoices) key = WorldAPI.operationKey();
                previousChoices = signature;
                busy = true; cancel.disabled = submit.disabled = true;
                for (const select of controls.values()) select.disabled = true;
                submit.textContent = '正在核对候选…'; error.textContent = '';
                try {
                    const result = await WorldAPI.json(`/api/ai/npc-reviews/${review.review_id}/resolve`, {choices, operation_key:key});
                    if (result.status === 'review_required') {render(result); key = WorldAPI.operationKey(); previousChoices = '';}
                    else finish(result);
                } catch (e) {error.textContent = e.message + ' 选择已保留，可重试或取消。';}
                finally {
                    busy = false; cancel.disabled = submit.disabled = false;
                    for (const select of controls.values()) select.disabled = false;
                    submit.textContent = '应用选择并继续';
                }
            };
        });
    }
    async function handle(result) {return result.status === 'review_required' ? resolve(result) : result;}
    function confirmAdditions(additions) {
        return new Promise(resolve => {
            const {panel, close} = dialog(`确认新增 ${additions.length} 名角色`);
            panel.append(element('p', '此分支会将以下人物登记到世界。请核对身份与新增理由。'));
            const list = element('ul', undefined, 'npc-review-list');
            for (const item of additions) {
                const li = element('li'); li.append(element('strong', item.name), element('p', item.reason)); list.append(li);
            }
            const actions = element('div', undefined, 'npc-review-actions');
            const cancel = element('button', '返回', 'btn btn-ghost');
            const confirm = element('button', '确认这些新增人物', 'btn btn-accent');
            const finish = value => {close(); resolve(value);};
            cancel.onclick = () => finish(false); confirm.onclick = () => finish(true);
            panel.addEventListener('cancel', event => {event.preventDefault(); finish(false);});
            actions.append(cancel, confirm); panel.append(list, actions); cancel.focus();
        });
    }
    return {handle, confirmAdditions, get active() {return opened;}};
})();
