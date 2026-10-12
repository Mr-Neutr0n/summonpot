export function sanitizeAnalyticsPayload(payload) {
  const campaignKey = /(?:^|_)(?:utm_[a-z0-9_]*|gclid|dclid|gbraid|wbraid|fbclid|fbc|fbp|msclkid|ttclid|twclid|li_fat_id|mc_cid|mc_eid|gclsrc|gad_source|igshid|rdt_cid|epik|qclid|sccid|irclid|_kx|campaign|adgroup|creative|keyword)(?:$|_)/i;
  const safeAcquisitionKeys = new Set(['$initial_url', '$initial_referrer', '$initial_referring_domain', '$referring_domain']);
  const urlKey = /(?:url|referrer|destination|href)/i;
  function canonical(value, key) {
    if (typeof value !== 'string') return value;
    if (/referrer/i.test(key)) {
      try { const u = new URL(value); return u.origin; } catch { return undefined; }
    }
    try {
      const absolute = /^https?:\/\//i.test(value);
      if (!absolute && !urlKey.test(key)) return value;
      const u = new URL(value, 'https://invalid.local');
      if (u.protocol !== 'http:' && u.protocol !== 'https:') return undefined;
      return `${absolute ? u.origin : ''}${u.pathname}`;
    } catch { return undefined; }
  }
  function clean(value, key = '') {
    if (value instanceof Date || value === null || typeof value !== 'object') {
      if (typeof value === 'string' && (/^https?:\/\//i.test(value) || urlKey.test(key))) return canonical(value, key);
      return value;
    }
    if (Array.isArray(value)) return value.map(item => clean(item, key)).filter(item => item !== undefined);
    const result = {};
    for (const [childKey, child] of Object.entries(value)) {
      if (campaignKey.test(childKey) || /^\$(?:session_entry|session_exit)_/i.test(childKey)) continue;
      if (/^\$(?:set|set_once)$/.test(childKey) && child && typeof child === 'object') {
        result[childKey] = Object.fromEntries(Object.entries(child).filter(([k]) => safeAcquisitionKeys.has(k)).map(([k, v]) => [k, clean(v, k)]).filter(([, v]) => v !== undefined));
        continue;
      }
      const sanitized = clean(child, childKey);
      if (sanitized !== undefined) result[childKey] = sanitized;
    }
    return result;
  }
  return clean(payload);
}

const privacyHook = `(${sanitizeAnalyticsPayload.toString()})`;

export const posthogScript = `!function(t,e){var o,n,p,r;e.__SV||(window.posthog=e,e._i=[],e.init=function(i,s,a){function g(t,e){var o=e.split(".");2==o.length&&(t=t[o[0]],e=o[1]),t[e]=function(){t.push([e].concat(Array.prototype.slice.call(arguments,0)))}}(p=t.createElement("script")).type="text/javascript",p.crossOrigin="anonymous",p.async=!0,p.src=s.api_host.replace(".i.posthog.com","-assets.i.posthog.com")+"/static/array.js",(r=t.getElementsByTagName("script")[0]).parentNode.insertBefore(p,r);var u=e;for(void 0!==a?u=e[a]=[]:a="posthog",u.people=u.people||[],u.toString=function(t){var e="posthog";return"posthog"!==a&&(e+="."+a),t||(e+=" (stub)"),e},u.people.toString=function(){return u.toString(1)+".people (stub)"},o="init capture identify alias people.set people.set_once people.unset reset opt_in_capturing opt_out_capturing has_opted_in_capturing has_opted_out_capturing clear_opt_in_out_capturing onFeatureFlags getFeatureFlag getFeatureFlagPayload isFeatureEnabled reloadFeatureFlags updateEarlyAccessFeatureEnrollment getEarlyAccessFeatures getSurveys getActiveMatchingSurveys renderSurvey canRenderSurvey captureException startSessionRecording stopSessionRecording sessionRecordingStarted capturePerformance captureTraceFeedback captureTraceMetric startExceptionCapture stopExceptionCapture".split(" "),n=0;n<o.length;n++)g(u,o[n]);e._i.push([i,s,a])},e.__SV=1)}(document,window.posthog||[]);posthog.init('phc_qXkp5FBQfrqHQwkqf3ys8iSoGoMYw2tpTHXGugXJhP8V',{api_host:'https://us.i.posthog.com',defaults:'2026-05-30',person_profiles:'identified_only',capture_pageview:true,capture_pageleave:true,autocapture:false,advanced_disable_flags:true,capture_performance:true,disable_session_recording:true,save_campaign_params:false,save_referrer:false,before_send:${privacyHook}});`;
export const posthogInitialization = posthogScript;

