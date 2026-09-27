// Which build is this?
//
// A stale bundle is invisible from the inside: the app looks fine, and a page
// added after that build simply is not there. Cost us an hour once — /operator
// rendered an empty content area on production because the production alias was
// pinned to an older deployment, and nothing on screen could say so.
//
// Vercel exposes the commit and branch to the BUILD, not to the browser, so
// vite.config.js pipes them in as VITE_* values. Absent (a plain local `npm run
// build`), the stamp says "dev" rather than inventing a commit.

const env = (typeof import.meta !== 'undefined' && import.meta.env) || {}

export const BUILD = {
  commit: env.VITE_COMMIT_SHA || '',
  ref: env.VITE_COMMIT_REF || '',
  builtAt: env.VITE_BUILT_AT || '',
}

/** "4b2217f · claude/credit-dispute-agent-u0yyq3" — enough to compare against git. */
export function buildStamp(build = BUILD) {
  const parts = []
  parts.push(build.commit ? build.commit.slice(0, 7) : 'dev build')
  if (build.ref) parts.push(build.ref)
  if (build.builtAt) parts.push(build.builtAt.slice(0, 10))
  return parts.join(' · ')
}

/**
 * What an unmatched route says.
 *
 * It names the path, because "blank" is indistinguishable from a crash, and it
 * names the build, because the usual reason a path that *should* exist does not
 * is that the deployment serving this page is older than the commit that added
 * it.
 */
export function notFoundMessage(path, build = BUILD) {
  return {
    title: 'No such page',
    path,
    stamp: buildStamp(build),
    hint: 'If this path should exist, this deployment is older than the commit '
      + 'that added it — check which build the production alias points at.',
  }
}
