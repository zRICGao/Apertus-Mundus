/* API boundary shared by the temporary GM and phone clients. No UI styling here. */
window.WorldAPI = (() => {
    const base = location.protocol === 'file:' ? 'http://127.0.0.1:8000' : location.origin;
    async function request(path, options) {
        try { return await fetch(path.startsWith(base + '/') ? path : base + path, options); }
        catch (error) {
            if (error.name !== 'TypeError') throw error;
            throw new Error('Cannot connect to the local game engine. Make sure the backend is running and open the page using its server address.');
        }
    }
    function responseError(response, data, text = '') {
        return new Error(typeof data?.detail === 'string' ? data.detail :
            typeof data?.message === 'string' ? data.message :
            `HTTP ${response.status}：${text.slice(0, 200) || 'Game engine request failed; please try again.'}`);
    }
    // Legacy callers still consume a Response; validate before they clear editor state.
    async function checkedRequest(path, options) {
        const response = await request(path, options);
        const text = await response.clone().text();
        let data;
        try { data = JSON.parse(text); } catch { /* Non-JSON errors retain their HTTP status. */ }
        if (!response.ok || data?.status === 'error') throw responseError(response, data, text);
        if (text && data === undefined) throw new Error('The game engine returned an invalid JSON response; try again.');
        return response;
    }
    async function json(path, body, method = body === undefined ? 'GET' : 'POST') {
        const response = await request(path, body === undefined ? {method} : {
            method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
        });
        const text = await response.text();
        let data;
        try { data = JSON.parse(text); }
        catch {
            throw new Error(response.ok ? 'The game engine returned an invalid JSON response.' :
                `HTTP ${response.status}：${text.slice(0, 200) || 'Game engine request failed'}`);
        }
        if (!response.ok || data?.status === 'error') throw responseError(response, data, text);
        if (data === null || typeof data !== 'object') throw new Error('The game engine returned an invalid JSON response.');
        return data;
    }
    async function stream(path, body, onText) {
        const response = await request(path, {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
        });
        if (!response.ok) {
            const text = await response.text();
            let data;
            try { data = JSON.parse(text); } catch { /* Keep non-JSON HTTP errors readable. */ }
            throw responseError(response, data, text);
        }
        if (!response.body) throw new Error('The game engine did not return streamed content; try again.');
        const reader = response.body.getReader(), decoder = new TextDecoder();
        let buffer = '', text = '', completed = false;
        try {
            while (true) {
                const {value, done} = await reader.read();
                buffer += decoder.decode(value, {stream: !done});
                const lines = buffer.split('\n');
                buffer = lines.pop();
                if (done && buffer.trim()) lines.push(buffer);
                for (const line of lines) {
                    if (!line.startsWith('data:')) continue;
                    const event = JSON.parse(line.slice(5).trim());
                    if (event.type === 'error') throw new Error(event.content || event.message || 'Generation failed');
                    if (event.type === 'text') { text += event.content; onText?.(text); }
                    if (event.type === 'done') { text = event.full_text ?? text; completed = true; onText?.(text); }
                }
                if (done) break;
            }
            if (!completed) throw new Error('Generation interrupted; the partial text was not saved.');
            return text;
        } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
    }
    // Clarification is explicit; names are display labels, UUIDs are the references sent back.
    async function decide(body) {
        const request = {...body, resolved_entities: {...body.resolved_entities}};
        if (typeof crypto !== 'undefined') request.operation_key ||= operationKey();
        for (let attempt = 0; attempt < 5; attempt++) {
            let result = await json('/api/ai/dynamic-options', request);
            if (result.status === 'review_required' && window.WorldNPCReview) result = await window.WorldNPCReview.handle(result);
            if (result.status !== 'clarification_required') return result;
            const candidates = result.candidates || [];
            const choice = prompt((result.question || 'Please clarify your action.') + '\n' +
                candidates.map((c, i) => `${i + 1}. ${c.name} (${c.location || c.type || ''})`).join('\n') +
                '\nChoose a number or enter a more specific action. Cancel to stop.');
            if (!choice?.trim()) return {status: 'cancelled'};
            const selected = /^\d+$/.test(choice.trim()) ? candidates[Number(choice) - 1] : null;
            if (selected) request.resolved_entities[result.mention || selected.name] = selected.entity_uid;
            else request.player_action = choice.trim();
            if (typeof crypto !== 'undefined') request.operation_key = operationKey();
        }
        throw new Error('Please make the action more specific and try again.');
    }
    function optionsFor(node, timelineId = null) {
        return (node?.all_options || node?.options || []).filter(option =>
            option.available !== false && (!option.is_generated || option.timeline_id === timelineId));
    }
    function operationKey() {
        // randomUUID requires HTTPS; getRandomValues also works on an HTTP LAN URL.
        if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
        const bytes = crypto.getRandomValues(new Uint8Array(16));
        bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
        const hex = [...bytes].map(value => value.toString(16).padStart(2, '0')).join('');
        return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
    }
    return {base, json, checkedRequest, stream, decide, optionsFor, operationKey};
})();
