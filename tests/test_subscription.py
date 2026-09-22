import json
import os
import subprocess
import unittest
from unittest.mock import patch

from qa_agents.agents import ClaudeSubscription


class SubscriptionTests(unittest.TestCase):
    def response(self, value, code=0):
        return subprocess.CompletedProcess([], code, json.dumps(value), '')

    def auth(self):
        return self.response({'loggedIn': True, 'authMethod': 'claude.ai'})

    def test_review_uses_subscription_without_tools_or_api_credentials(self):
        result = {'checks': ['unit'], 'gaps': []}
        response = self.response({'subtype': 'success', 'structured_output': result,
                                 'usage': {'input_tokens': 10}, 'modelUsage': {'fixture-model': {}}})
        agent = ClaudeSubscription('sonnet')
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test', 'ANTHROPIC_BASE_URL': 'test', 'OPENAI_API_KEY': 'test'}), patch(
                'qa_agents.agents.subprocess.run', side_effect=[self.auth(), response]) as run:
            self.assertEqual(agent.ask('beacon', {'brief': 'fixture'}), result)
        args, kwargs = run.call_args
        self.assertEqual(args[0][args[0].index('--tools')+1], '')
        self.assertEqual(args[0][args[0].index('--allowedTools')+1], 'StructuredOutput')
        self.assertIn('--no-session-persistence', args[0])
        self.assertEqual(kwargs['input'], json.dumps({'brief': 'fixture'}))
        self.assertFalse({'ANTHROPIC_API_KEY', 'ANTHROPIC_BASE_URL', 'OPENAI_API_KEY'} & kwargs['env'].keys())
        self.assertEqual(agent.identity, {'provider': 'claude-subscription', 'model': 'sonnet'})
        self.assertEqual(agent.usage[0]['modelUsage'], {'fixture-model': {}})

    def test_api_auth_cannot_run_or_fall_back(self):
        for auth in ({'loggedIn': True, 'authMethod': 'api_key'}, {'loggedIn': False}, []):
            with self.subTest(auth=auth), patch('qa_agents.agents.subprocess.run', return_value=self.response(auth)) as run:
                with self.assertRaises(ValueError):
                    ClaudeSubscription('sonnet').ask('beacon', {})
                run.assert_called_once()

    def test_incomplete_and_denied_reviews_fail_without_retry(self):
        for response in ({'subtype': 'error_max_turns'}, {'subtype': 'success', 'structured_output': {}, 'is_error': True},
                         {'subtype': 'success', 'structured_output': {}, 'permission_denials': [{'tool_name': 'Read'}]}):
            with self.subTest(response=response), patch('qa_agents.agents.subprocess.run',
                    side_effect=[self.auth(), self.response(response)]) as run:
                with self.assertRaises(ValueError):
                    ClaudeSubscription('sonnet').ask('beacon', {})
                self.assertEqual(run.call_count, 2)

    def test_timeout_and_oversized_context_are_bounded(self):
        with patch('qa_agents.agents.subprocess.run', side_effect=[self.auth(), subprocess.TimeoutExpired('claude', 180)]) as run:
            with self.assertRaisesRegex(ValueError, 'timed out'):
                ClaudeSubscription('sonnet').ask('beacon', {})
            self.assertEqual(run.call_count, 2)
        with patch('qa_agents.agents.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, '180 KB'):
                ClaudeSubscription('sonnet').ask('beacon', {'text': 'x'*180001})
            run.assert_not_called()
