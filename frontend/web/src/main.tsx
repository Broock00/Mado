import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createBrowserRouter, RouterProvider } from 'react-router-dom'

import './styles/tokens.css'
import { AppShell } from '@/app/AppShell'
import { DiscoverPage } from '@/features/discover/DiscoverPage'
import { SearchPage } from '@/features/search/SearchPage'
import { ExperienceDetailPage } from '@/features/experiences/ExperienceDetailPage'
import { SavedPage } from '@/features/saved/SavedPage'
import { SignInPage } from '@/features/auth/SignInPage'
import { MyPostsPage } from '@/features/publishing/MyPostsPage'
import { ComposePage } from '@/features/publishing/ComposePage'
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
      { path: 'experiences/:experienceId', element: <ExperienceDetailPage /> },
      { path: 'saved', element: <SavedPage /> },
      { path: 'posts', element: <MyPostsPage /> },
      { path: 'compose', element: <ComposePage /> },
      { path: 'compose/:experienceId', element: <ComposePage /> },
      { path: '*', element: <NotFoundPage /> },
    ],
  },
  { path: '/signin', element: <SignInPage /> },
])

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
)
