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
        if (opened) throw new Error('Complete the current character review first.');
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
            const {panel, close} = dialog(maps ? (review.entries.some(e=>e.kind!=='map') ? 'Review world changes' : 'Review map changes') : 'Review introduced characters');
            const intro = element('p', review.message);
            const list = element('div', undefined, 'npc-review-list');
            const error = element('p', '', 'npc-review-error'); error.setAttribute('role', 'alert');
            const actions = element('div', undefined, 'npc-review-actions');
            const cancel = element('button', 'Cancel this generation', 'btn btn-ghost');
            const submit = element('button', 'Apply choices and continue', 'btn btn-accent');
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
                    section.append(element('legend', `Branch ${item.branch + 1} · ${item.name}`));
                    const isMap=item.kind==='map';
                    section.append(element('p', item.description || (isMap ? 'No area description available' : 'No full profile available')));
                    section.append(element('p', item.reason || (isMap ? "Review the location's identity and whether it is needed." : 'Review whether this character needs to be registered.'), 'npc-review-reason'));
                    const select = element('select'); select.required = true;
                    select.setAttribute('aria-label', `${item.name} handling option`);
                    const add = (value, label) => {const opt = element('option', label); opt.value = value; select.append(opt);};
                    add('', 'Select how to handle this');
                    for (const candidate of item.candidates || []) {
                        if(isMap && !Object.hasOwn(item.options,candidate.choice)) continue;
                        add(isMap ? candidate.choice : 'reuse:' + candidate.entity_uid,
                            `${isMap && candidate.choice.startsWith('reveal:') ? 'Reveal existing area' : 'Reuse'}：${candidate.name} · ${candidate.location || 'Location not recorded'}${isMap ? (candidate.connection ? ' · Recorded passage exists' : ' · No direct passage') : ''}`);
                    }
                    if(!isMap) for (const other of data.entries)
                        if (other.kind!=='map' && other.branch === item.branch && other.key < item.key)
                            add('same:' + other.ref, `Same person as this branch's ${other.name}`);
                    if(!isMap || Object.hasOwn(item.options,'create')) add('create',isMap ? 'Explicitly register a distinct new area' : 'Explicitly register as a different new character');
                    add('omit',isMap ? 'Cancel this map change' : 'Cancel addition / background description only');
                    if (item.decision !== 'unknown') select.value = item.decision;
                    section.append(select);
                    if(isMap && item.mode==='new') {
                        const arrival=element('select'); arrival.required=true;
                        arrival.setAttribute('aria-label',`${item.name}: enter destination?`);
                        for(const [value,label] of [['',"Choose the team's final position"],['stay','Discover area only; the team stays in place'],['enter','The team enters the destination in this branch']]) {
                            const option=element('option',label); option.value=value; arrival.append(option);
                        }
                        if(item.arrival!=='unknown') arrival.value=item.arrival;
                        select.addEventListener('change',()=>{if(select.value==='omit') arrival.value='stay';});
                        section.append(arrival); controls.set(item.key+':arrival',arrival);
                    }
                    const details = element('details'); details.append(element('summary',isMap ? 'View existing location details' : 'View existing identity details'));
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
                submit.textContent = 'Reviewing candidates...'; error.textContent = '';
                try {
                    const result = await WorldAPI.json(`/api/ai/npc-reviews/${review.review_id}/resolve`, {choices, operation_key:key});
                    if (result.status === 'review_required') {render(result); key = WorldAPI.operationKey(); previousChoices = '';}
                    else finish(result);
                } catch (e) {error.textContent = e.message + ' Choices have been preserved; retry or cancel.';}
                finally {
                    busy = false; cancel.disabled = submit.disabled = false;
                    for (const select of controls.values()) select.disabled = false;
                    submit.textContent = 'Apply choices and continue';
                }
            };
        });
    }
    async function handle(result) {return result.status === 'review_required' ? resolve(result) : result;}
    function confirmAdditions(additions) {
        return new Promise(resolve => {
            const {panel, close} = dialog(`Confirm addition of ${additions.length} characters`);
            panel.append(element('p', 'This branch will register the following people in the world. Review their identities and the reasons for adding them.'));
            const list = element('ul', undefined, 'npc-review-list');
            for (const item of additions) {
                const li = element('li'); li.append(element('strong', item.name), element('p', item.reason)); list.append(li);
            }
            const actions = element('div', undefined, 'npc-review-actions');
            const cancel = element('button', 'Back', 'btn btn-ghost');
            const confirm = element('button', 'Confirm these new characters', 'btn btn-accent');
            const finish = value => {close(); resolve(value);};
            cancel.onclick = () => finish(false); confirm.onclick = () => finish(true);
            panel.addEventListener('cancel', event => {event.preventDefault(); finish(false);});
            actions.append(cancel, confirm); panel.append(list, actions); cancel.focus();
        });
    }
    return {handle, confirmAdditions, get active() {return opened;}};
})();
