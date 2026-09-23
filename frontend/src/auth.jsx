// Frontend authentication, using Clerk as the managed provider.
//
// Clerk's *publishable* key (VITE_CLERK_PUBLISHABLE_KEY) is a browser-safe
// public credential — the only thing auth needs in the frontend. No secret,
// API key, or private key is ever exposed here. The backend verifies the
// session token Clerk issues against Clerk's public JWKS; this file only
// signs the user in and forwards that token to the API.
//
// When the key is absent (local dev), the app runs unauthenticated to match
// the backend's AUTH_MODE=disabled — no sign-in wall, single local user.
import {
  ClerkProvider, SignIn, SignedIn, SignedOut, UserButton, useAuth,
} from '@clerk/clerk-react'
import { setTokenGetter } from './api'

export const CLERK_KEY = import.meta.env.VITE_CLERK_PUBLISHABLE_KEY || ''
export const authEnabled = Boolean(CLERK_KEY)

// Bridges Clerk's token into the api module so every request carries it. Set
// during render (not an effect) so the getter is in place before any child's
// data fetch runs. getToken() returns a fresh, auto-refreshed token each call.
function TokenBridge() {
  const { getToken } = useAuth()
  setTokenGetter(() => getToken())
  return null
}

export function AuthProvider({ children }) {
  if (!authEnabled) return children
  return (
    <ClerkProvider publishableKey={CLERK_KEY} afterSignOutUrl="/">
      {children}
    </ClerkProvider>
  )
}

// A full-screen, mobile-first sign-in view in the app's own visual language.
function SignInScreen() {
  return (
    <div className="auth-screen">
      <div className="auth-card">
        <div className="brand brand-center">Credit Sniper<small>Credit intelligence</small></div>
        <p className="auth-sub">Sign in to view your reports, cases, and disputes.</p>
        <SignIn routing="hash" appearance={{ elements: { rootBox: 'auth-clerk', card: 'auth-clerk-card' } }} />
      </div>
    </div>
  )
}

// Gates the app: shows a loading state, the sign-in screen when signed out,
// and the app once signed in. Passthrough when auth is disabled.
export function RequireAuth({ children }) {
  if (!authEnabled) return children
  return (
    <>
      <TokenBridge />
      <SignedIn>{children}</SignedIn>
      <SignedOut><SignInScreen /></SignedOut>
    </>
  )
}

// Sign-out / account control for the Profile screen. Renders nothing when
// auth is disabled (there's no session to end).
export function AccountControl() {
  if (!authEnabled) return null
  return (
    <div className="account-control">
      <UserButton showName afterSignOutUrl="/" />
    </div>
  )
}
