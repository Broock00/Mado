import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createBrowserRouter, RouterProvider } from 'react-router-dom'

import './styles/tokens.css'
import { AppShell } from '@/app/AppShell'
import { LanguageProvider } from '@/app/language'
import { registerServiceWorker } from '@/app/offline'
import { DiscoverPage } from '@/features/discover/DiscoverPage'
import { SearchPage } from '@/features/search/SearchPage'
import { ExperienceDetailPage } from '@/features/experiences/ExperienceDetailPage'
import { SavedPage } from '@/features/saved/SavedPage'
import { SignInPage } from '@/features/auth/SignInPage'
import { VerifyEmailPage } from '@/features/auth/VerifyEmailPage'
import { ForgotPasswordPage } from '@/features/auth/ForgotPasswordPage'
import { ResetPasswordPage } from '@/features/auth/ResetPasswordPage'
import { MyPostsPage } from '@/features/publishing/MyPostsPage'
import { PublisherDashboard } from '@/features/analytics/PublisherDashboard'
import { ComposePage } from '@/features/publishing/ComposePage'
import { PlanPage } from '@/features/planning/PlanPage'
import { ItineraryPage } from '@/features/planning/ItineraryPage'
import { SettingsPage } from '@/features/settings/SettingsPage'
import { CollectionsPage } from '@/features/collections/CollectionsPage'
import { CollectionDetailPage } from '@/features/collections/CollectionDetailPage'
import { ModerationPage } from '@/features/moderation/ModerationPage'
import { BookingsPage } from '@/features/commerce/BookingsPage'
import { OrderDetailPage, OrdersPage } from '@/features/commerce/OrdersPage'
import { DeveloperPage } from '@/features/developer/DeveloperPage'
import { NotFoundPage } from '@/features/NotFoundPage'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Discovery data is time-sensitive but not per-second; refetching on every
      // window focus would churn the feed while an explorer switches tabs.
      refetchOnWindowFocus: false,
      staleTime: 30_000,
      retry: 1,
    },
  },
})

const router = createBrowserRouter([
  {
    path: '/',
    element: <AppShell />,
    children: [
      { index: true, element: <DiscoverPage /> },
      { path: 'search', element: <SearchPage /> },
      { path: 'collections', element: <CollectionsPage /> },
      { path: 'posts/analytics', element: <PublisherDashboard /> },
      // Public: a shared link has to work for someone who has never signed in.
      { path: 'collections/:collectionId', element: <CollectionDetailPage /> },
      // Reached from a link in an email, so they live inside the shell: someone
      // arriving here has a header to navigate away from, and a page with no way
      // out is how a confirmation flow turns into a support request.
      { path: 'verify-email', element: <VerifyEmailPage /> },
      { path: 'forgot-password', element: <ForgotPasswordPage /> },
      { path: 'reset-password', element: <ResetPasswordPage /> },
      { path: 'experiences/:experienceId', element: <ExperienceDetailPage /> },
      { path: 'saved', element: <SavedPage /> },
      { path: 'orders', element: <OrdersPage /> },
      { path: 'orders/:orderId', element: <OrderDetailPage /> },
      { path: 'posts', element: <MyPostsPage /> },
      { path: 'posts/:experienceId/bookings', element: <BookingsPage /> },
      { path: 'compose', element: <ComposePage /> },
      { path: 'compose/:experienceId', element: <ComposePage /> },
      { path: 'plans', element: <PlanPage /> },
      { path: 'plans/:itineraryId', element: <ItineraryPage /> },
      { path: 'settings', element: <SettingsPage /> },
      { path: 'moderation', element: <ModerationPage /> },
      { path: 'developers', element: <DeveloperPage /> },
      { path: '*', element: <NotFoundPage /> },
    ],
  },
  { path: '/signin', element: <SignInPage /> },
])

// Caches the app shell and any itinerary the explorer kept, so a plan is
// readable on a street with no data (spec EXP-005). Production only - see the
// note in `app/offline.ts`.
registerServiceWorker()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      {/* Inside the query client, because changing language writes to the
          profile; outside the router, so every route can read it. */}
      <LanguageProvider>
        <RouterProvider router={router} />
      </LanguageProvider>
    </QueryClientProvider>
  </StrictMode>,
)
