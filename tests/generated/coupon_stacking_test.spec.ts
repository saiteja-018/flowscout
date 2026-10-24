import { test, expect } from '@playwright/test';

test.describe('SAVE20 coupon stacking reproduction', () => {
  test.beforeEach(async ({ page }) => {
    const fixed = process.env.FLOWSCOUT_TEST_MODE === 'fixed';
    await page.context().request.post('/shop/api/mode', { data: { fixed } });
    await page.context().request.post('/shop/api/reset');
  });

  test('six applications of SAVE20 produce a negative total', async ({ page }) => {
    await page.goto('/shop/checkout');
    await expect(page.getByTestId('cart-total')).toHaveAttribute('data-value', '129.97');

    for (let attempt = 1; attempt <= 6; attempt += 1) {
      await page.getByTestId('coupon-input').fill('SAVE20');
      await page.getByTestId('apply-coupon-btn').click();
      await expect(page.getByTestId('cart-total')).toBeVisible();
    }

    await expect(page.getByTestId('applied-coupons')).toContainText('SAVE20 x6');
    const total = Number(await page.getByTestId('cart-total').getAttribute('data-value'));
    expect(total).toBeLessThan(0);
    await expect(page.getByTestId('cart-total')).toHaveClass(/negative/);
  });
});
