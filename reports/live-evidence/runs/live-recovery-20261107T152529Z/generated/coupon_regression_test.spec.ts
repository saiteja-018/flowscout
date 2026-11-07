import { test, expect } from '@playwright/test';

test.describe('SAVE20 duplicate-coupon regression', () => {
  test.beforeEach(async ({ page }) => {
    const fixed = process.env.FLOWSCOUT_TEST_MODE !== 'broken';
    await page.context().request.post('/shop/api/mode', { data: { fixed } });
    await page.context().request.post('/shop/api/reset');
  });

  test('duplicate SAVE20 is rejected and the total stays non-negative', async ({ page }) => {
    await page.goto('/shop/checkout');

    await page.getByTestId('coupon-input').fill('SAVE20');
    await page.getByTestId('apply-coupon-btn').click();
    await expect(page.getByTestId('cart-total')).toHaveAttribute('data-value', '103.98');

    await page.getByTestId('coupon-input').fill('SAVE20');
    await page.getByTestId('apply-coupon-btn').click();
    await expect(page.getByTestId('coupon-result')).toContainText('already been applied');

    const total = Number(await page.getByTestId('cart-total').getAttribute('data-value'));
    expect(total).toBeGreaterThanOrEqual(0);
    await expect(page.getByTestId('applied-coupons')).toContainText('SAVE20 x1');
  });
});
