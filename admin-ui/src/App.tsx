import { useEffect } from 'react';
import { Link, Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { getShopify } from './appBridge';
import { ErrorBanner } from './components/Feedback';
import { AllocationRuleEditPage, AllocationRulesPage } from './pages/AllocationRules';
import { AutomationLogsPage } from './pages/AutomationLogs';
import { CarrierAccountsPage } from './pages/CarrierAccounts';
import { CarrierDetailPage } from './pages/CarrierDetail';
import { CarriersPage } from './pages/Carriers';
import { DashboardPage } from './pages/Dashboard';
import { ManualReviewPage } from './pages/ManualReview';
import { OrderDetailPage } from './pages/OrderDetail';
import { OrdersPage } from './pages/Orders';
import { SettingsPage } from './pages/Settings';
import { ShipmentsPage } from './pages/Shipments';
import { TrackingPage } from './pages/Tracking';
import { WarehouseEditPage, WarehousesPage } from './pages/Warehouses';
import { useSession } from './session';

const NAV_ITEMS: { to: string; label: string }[] = [
  { to: '/orders', label: 'Orders' },
  { to: '/shipments', label: 'Shipments' },
  { to: '/manual-review', label: 'Manual Review' },
  { to: '/carriers', label: 'Carriers' },
  { to: '/carrier-accounts', label: 'Carrier Accounts' },
  { to: '/warehouses', label: 'Warehouses' },
  { to: '/allocation-rules', label: 'Allocation Rules' },
  { to: '/tracking', label: 'Tracking' },
  { to: '/automation-logs', label: 'Automation Logs' },
  { to: '/settings', label: 'Settings' },
];

/**
 * App Bridge admin navigation. App Bridge clicks the matching anchor when an item is selected
 * in the admin sidebar, so react-router `<Link>`s give client-side navigation. The `rel="home"`
 * link is the app's landing page (Dashboard) and is not shown as a separate item.
 */
function NavMenu() {
  if (!getShopify()) return <DevNav />;
  return (
    <ui-nav-menu>
      <Link to="/" rel="home">
        Dashboard
      </Link>
      {NAV_ITEMS.map((item) => (
        <Link key={item.to} to={item.to}>
          {item.label}
        </Link>
      ))}
    </ui-nav-menu>
  );
}

/** Outside the Shopify admin (local dev without App Bridge) show the same links inline. */
function DevNav() {
  return (
    <s-box padding="small" background="subdued" borderWidth="none none base none" borderColor="base">
      <s-stack direction="inline" gap="base" alignItems="center">
        <s-badge tone="caution">Dev mode (not embedded)</s-badge>
        <s-link href="/">Dashboard</s-link>
        {NAV_ITEMS.map((item) => (
          <s-link key={item.to} href={item.to}>
            {item.label}
          </s-link>
        ))}
      </s-stack>
    </s-box>
  );
}

/**
 * Polaris `<s-link href>` / `<s-button href>` dispatch a cancelable `shopify:navigate` event for
 * same-origin hrefs. Reading the href and routing client-side avoids a full page reload.
 */
function usePolarisNavigation() {
  const navigate = useNavigate();
  useEffect(() => {
    const handler = (event: Event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      const href = target.getAttribute('href');
      if (!href || !href.startsWith('/') || href.startsWith('//')) return;
      event.preventDefault();
      navigate(href);
    };
    document.addEventListener('shopify:navigate', handler);
    return () => document.removeEventListener('shopify:navigate', handler);
  }, [navigate]);
}

/** `/logs?…` is an alias of the Automation Logs page (the nav item lives at `/automation-logs`). */
function LogsAlias() {
  const location = useLocation();
  return <Navigate to={{ pathname: '/automation-logs', search: location.search }} replace />;
}

function NotFoundPage() {
  return (
    <s-page heading="Page not found">
      <s-section>
        <s-paragraph>
          This page does not exist. Go back to the <s-link href="/">Dashboard</s-link>.
        </s-paragraph>
      </s-section>
    </s-page>
  );
}

export function App() {
  usePolarisNavigation();
  const { error: sessionError, reload } = useSession();

  return (
    <>
      <NavMenu />
      {sessionError && (
        <s-box padding="base">
          <ErrorBanner heading="Could not load your session" error={sessionError} onRetry={() => void reload()} />
        </s-box>
      )}
      <Routes>
        <Route path="/" element={<DashboardPage />} />
        <Route path="/orders" element={<OrdersPage />} />
        <Route path="/orders/:orderId" element={<OrderDetailPage />} />
        <Route path="/shipments" element={<ShipmentsPage />} />
        <Route path="/manual-review" element={<ManualReviewPage />} />
        <Route path="/carriers" element={<CarriersPage />} />
        <Route path="/carriers/:code" element={<CarrierDetailPage />} />
        <Route path="/carrier-accounts" element={<CarrierAccountsPage />} />
        <Route path="/warehouses" element={<WarehousesPage />} />
        <Route path="/warehouses/new" element={<WarehouseEditPage />} />
        <Route path="/warehouses/:id" element={<WarehouseEditPage />} />
        <Route path="/allocation-rules" element={<AllocationRulesPage />} />
        <Route path="/allocation-rules/new" element={<AllocationRuleEditPage />} />
        <Route path="/allocation-rules/:id" element={<AllocationRuleEditPage />} />
        <Route path="/tracking" element={<TrackingPage />} />
        <Route path="/automation-logs" element={<AutomationLogsPage />} />
        <Route path="/logs" element={<LogsAlias />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Routes>
    </>
  );
}
