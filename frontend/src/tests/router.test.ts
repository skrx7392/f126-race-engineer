import { describe, it, expect } from 'vitest';
import { Router, href, intParam, isAnalysisRoute, parseHash } from '../lib/router.svelte';

/**
 * Route parsing is a pure function, so it is tested as one — no DOM, no
 * navigation, no component. The cases that matter are the malformed ones: a
 * hash-routed SPA is handed whatever a user pasted, and every one of these
 * shapes has to resolve to a route rather than throw or half-match.
 */

describe('parseHash', () => {
  it('treats the empty hash and bare slash as the pit wall', () => {
    for (const input of ['', '#', '#/', '/', '#//']) {
      expect(parseHash(input).name, input).toBe('pitwall');
    }
  });

  it('accepts a hash with or without the leading marker', () => {
    expect(parseHash('#/sessions').name).toBe('sessions');
    expect(parseHash('/sessions').name).toBe('sessions');
    expect(parseHash('sessions').name).toBe('sessions');
  });

  it('captures a session id', () => {
    const route = parseHash('#/sessions/42');
    expect(route.name).toBe('session');
    expect(route.params['id']).toBe('42');
    expect(route.path).toBe('/sessions/42');
  });

  it('refuses a session id that is not digits', () => {
    for (const bad of ['#/sessions/abc', '#/sessions/-1', '#/sessions/1.5', '#/sessions/1a']) {
      expect(parseHash(bad).name, bad).toBe('notfound');
    }
  });

  it('refuses extra segments rather than guessing', () => {
    expect(parseHash('#/sessions/42/laps').name).toBe('notfound');
    expect(parseHash('#/compare/3').name).toBe('notfound');
  });

  it('routes the three analysis views', () => {
    expect(parseHash('#/compare').name).toBe('compare');
    expect(parseHash('#/corners').name).toBe('corners');
    expect(parseHash('#/stints').name).toBe('stints');
  });

  it('parses the query string separately from the path', () => {
    const route = parseHash('#/compare?sa=3&la=12&sb=3&lb=15');
    expect(route.name).toBe('compare');
    expect(route.path).toBe('/compare');
    expect(route.query.get('sa')).toBe('3');
    expect(route.query.get('lb')).toBe('15');
  });

  it('always supplies a query object, even with no query', () => {
    expect([...parseHash('#/sessions').query.keys()]).toEqual([]);
  });

  it('tolerates a trailing slash and an empty query', () => {
    expect(parseHash('#/sessions/').name).toBe('sessions');
    expect(parseHash('#/sessions?').name).toBe('sessions');
  });

  it('keeps a query attached to a session detail route', () => {
    const route = parseHash('#/sessions/7?car=21');
    expect(route.name).toBe('session');
    expect(route.params['id']).toBe('7');
    expect(route.query.get('car')).toBe('21');
  });

  it('sends unknown paths to notfound', () => {
    expect(parseHash('#/nope').name).toBe('notfound');
    expect(parseHash('#/sessions/1/2/3').name).toBe('notfound');
  });
});

describe('isAnalysisRoute', () => {
  it('covers every route except the pit wall', () => {
    expect(isAnalysisRoute('pitwall')).toBe(false);
    for (const name of ['sessions', 'session', 'compare', 'corners', 'stints', 'notfound'] as const) {
      expect(isAnalysisRoute(name), name).toBe(true);
    }
  });
});

describe('href', () => {
  it('builds a bare hash path with no query', () => {
    expect(href('/sessions')).toBe('#/sessions');
    expect(href('/sessions', {})).toBe('#/sessions');
  });

  it('drops null, undefined and empty values so links never carry blanks', () => {
    expect(href('/compare', { sa: 3, la: null, sb: undefined, lb: '' })).toBe('#/compare?sa=3');
  });

  it('keeps zero, which is a real car index', () => {
    expect(href('/sessions/1', { car: 0 })).toBe('#/sessions/1?car=0');
  });

  it('round-trips through parseHash', () => {
    const link = href('/compare', { sa: 3, la: 12, sb: 4, lb: 9 });
    const route = parseHash(link);
    expect(route.name).toBe('compare');
    expect(intParam(route.query, 'sa')).toBe(3);
    expect(intParam(route.query, 'lb')).toBe(9);
  });
});

describe('intParam', () => {
  it('reads digits and rejects everything else', () => {
    const q = new URLSearchParams('a=3&b=abc&c=-1&d=1.5&e=');
    expect(intParam(q, 'a')).toBe(3);
    expect(intParam(q, 'b')).toBeNull();
    expect(intParam(q, 'c')).toBeNull();
    expect(intParam(q, 'd')).toBeNull();
    expect(intParam(q, 'e')).toBeNull();
    expect(intParam(q, 'missing')).toBeNull();
  });
});

describe('Router', () => {
  it('starts on the route it is constructed with', () => {
    expect(new Router('#/stints').route.name).toBe('stints');
  });

  it('follows hashchange once started, and stops cleanly', () => {
    const router = new Router();
    const stop = router.start();
    try {
      window.location.hash = '#/sessions/5';
      window.dispatchEvent(new HashChangeEvent('hashchange'));
      expect(router.route.name).toBe('session');
      expect(router.route.params['id']).toBe('5');
    } finally {
      stop();
      window.location.hash = '';
    }
  });

  it('ignores hashchange after stopping', () => {
    const router = new Router();
    const stop = router.start();
    stop();
    window.location.hash = '#/corners';
    window.dispatchEvent(new HashChangeEvent('hashchange'));
    expect(router.route.name).not.toBe('corners');
    window.location.hash = '';
  });

  it('navigates programmatically, and re-navigating to the same hash is idempotent', () => {
    const router = new Router();
    const stop = router.start();
    try {
      router.go('#/compare');
      window.dispatchEvent(new HashChangeEvent('hashchange'));
      expect(router.route.name).toBe('compare');

      // Assigning an identical hash fires no event; the router must still be
      // holding the right route rather than going stale.
      router.go('#/compare');
      expect(router.route.name).toBe('compare');
    } finally {
      stop();
      window.location.hash = '';
    }
  });

  it('accepts a path without the hash marker', () => {
    const router = new Router();
    const stop = router.start();
    try {
      router.go('/stints');
      window.dispatchEvent(new HashChangeEvent('hashchange'));
      expect(router.route.name).toBe('stints');
    } finally {
      stop();
      window.location.hash = '';
    }
  });
});
