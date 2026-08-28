import asyncio
import os
import time

from pr_agent.algo.ai_handlers.langchain_ai_handler import LangChainOpenAIHandler
from pr_agent.config_loader import get_settings


def check_settings():
    print('Checking settings...')
    settings = get_settings()
    
    # Check OpenAI settings
    if not hasattr(settings, 'openai'):
        print('OpenAI settings not found')
        return False
    
    if not hasattr(settings.openai, 'key'):
        print('OpenAI API key not found')
        return False
    
    print('OpenAI API key found')
    return True

async def measure_performance(handler, num_requests=3):
    print(f'\nRunning performance test with {num_requests} requests...')
    start_time = time.time()
    
    # Create multiple requests
    tasks = [
        handler.chat_completion(
            model='gpt-3.5-turbo',
            system='You are a helpful assistant',
            user=f'Test message {i}',
            temperature=0.2
        ) for i in range(num_requests)
    ]
    
    # Execute requests concurrently
    responses = await asyncio.gather(*tasks)
    
    end_time = time.time()
    total_time = end_time - start_time
    avg_time = total_time / num_requests
    
    print(f'Performance results:')
    print(f'Total time: {total_time:.2f} seconds')
    print(f'Average time per request: {avg_time:.2f} seconds')
    print(f'Requests per second: {num_requests/total_time:.2f}')
    
    return responses

async def test():
    print('Starting test...')
    
    # Check settings first
    if not check_settings():
        print('Please set up your environment variables or configuration file')
        print('Required: OPENAI_API_KEY')
        return
    
    try:
        handler = LangChainOpenAIHandler()
        print('Handler created')
        
        # Basic functionality test
        response = await handler.chat_completion(
            model='gpt-3.5-turbo',
            system='You are a helpful assistant',
            user='Hello',
            temperature=0.2,
            img_path='test.jpg'
        )
        print('Response:', response)
        
        # Performance test
        await measure_performance(handler)
        
    except Exception as e:
        print('Error:', str(e))
        print('Error type:', type(e))
        print('Error details:', e.__dict__ if hasattr(e, '__dict__') else 'No additional details')

if __name__ == '__main__':
    print('Environment variables:')
    print('OPENAI_API_KEY:', 'Set' if os.getenv('OPENAI_API_KEY') else 'Not set')
    print('OPENAI_API_TYPE:', os.getenv('OPENAI_API_TYPE', 'Not set'))
    print('OPENAI_API_BASE:', os.getenv('OPENAI_API_BASE', 'Not set'))
    
    asyncio.run(test()) 
  
    