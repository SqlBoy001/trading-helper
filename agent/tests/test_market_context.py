import unittest
from src.personal_web.market_context import compare,validate_pool,parse_quote

class MarketContextTests(unittest.TestCase):
 def test_relative_return_is_not_position_pnl(self):
  stock=dict(change_pct=2.23,closing_date_verified=True)
  board=dict(change_pct=-.18,closing_date_verified=True)
  self.assertEqual(compare(stock,board),dict(status='against_up',excess_pp=2.41))
  self.assertEqual(compare(board,stock)['status'],'against_down')
 def test_missing_stale_and_flat(self):
  q=dict(change_pct=0,closing_date_verified=True)
  self.assertEqual(compare(q,q)['status'],'same_or_flat')
  self.assertEqual(compare(q,dict(change_pct=2,closing_date_verified=False))['status'],'unknown')
  self.assertEqual(compare(q,dict(change_pct=float('nan'),closing_date_verified=True))['status'],'unknown')
 def test_pool_completeness_and_date(self):
  p=dict(rc=0,data=dict(qdate=20260922,tc=1,pool=[]))
  with self.assertRaises(ValueError):validate_pool(p,'2026-09-22')
  p['data']['tc']=0
  self.assertEqual(validate_pool(p,'2026-09-22')['count'],0)
  with self.assertRaises(ValueError):validate_pool(p,'2026-09-21')
 def test_duplicates_not_complete(self):
  with self.assertRaises(ValueError):validate_pool(dict(rc=0,data=dict(qdate=20260922,tc=2,pool=[dict(c='X',m=0)]*2)),'2026-09-22')
 def test_quote_dates(self):
  self.assertTrue(parse_quote(dict(f124=1790062771,f3=1),'2026-09-22')['closing_date_verified'])
  self.assertFalse(parse_quote(dict(f124=1790062771,f3=1),'2026-09-21')['closing_date_verified'])
if __name__=='__main__':unittest.main()
