import torch
from torch.testing._internal.common_utils import TestCase, run_tests
from torch.testing._internal.logging_tensor import LoggingTensor, capture_logs, log_input

def are_aliased(x, y):
    if x._base is None and y._base is None:
        return False
    if x._base is not None and y._base is None:
        return x._base is y
    if x._base is None and y._base is not None:
        return y._base is x
    return x._base is y._base


class TestFunctionalization(TestCase):

    def get_logs(self, func, inpt):
        input_clone_logging = LoggingTensor(inpt.clone())
        input_functional_logging = torch._to_functional_tensor(input_clone_logging)

        with capture_logs() as logs:
            torch._enable_functionalization()
            try:
                func(input_functional_logging)
            finally:
                torch._disable_functionalization()
        return logs

    def assert_functionalization(self, func, inpt):
        input_clone = inpt.clone()
        input_clone2 = inpt.clone()
        input_functional = torch._to_functional_tensor(input_clone2)

        # Compare outputs (and mutated inputs), with and without functionalization.
        out_ref = func(inpt)

        torch._enable_functionalization()
        try:
            out_functional = func(input_functional)
        finally:
            torch._disable_functionalization()

        # We need to sync the input tensors first, in case there are any queued mutations left.
        torch._sync(input_functional)
        torch._sync(out_functional)
        self.assertEqual(out_ref, torch._from_functional_tensor(out_functional))
        self.assertEqual(inpt, torch._from_functional_tensor(input_functional))  # input mutations should still occur

    def test_simple(self):
        def f(x):
            # simple test: 1 view op, 1 inplace op
            tmp = torch.ones(4, 2)
            y = x.view(4, 2)
            y.add_(tmp)
            z = x * x
            return y
        self.assert_functionalization(f, torch.ones(4, 2))
        logs = self.get_logs(f, torch.ones(4, 2))
        self.assertExpectedInline('\n'.join(logs), """\
$1 = torch._ops.aten.view($0, [4, 2])
$2 = torch._ops.aten.add($1, tensor([[1., 1.],
        [1., 1.],
        [1., 1.],
        [1., 1.]]))
$3 = torch._ops.aten.view($2, [4, 2])
$4 = torch._ops.aten.mul($3, $3)""")

    def test_diagonal(self):
        def f(x):
            # test: view ops that take a subset of the original tensor (select/diagonal)
            tmp = torch.ones(2)
            y = x.diagonal()
            y.add_(tmp)
            z = x * x
            return z
        self.assert_functionalization(f, torch.ones(2, 2))
        logs = self.get_logs(f, torch.ones(2, 2))
        self.assertExpectedInline('\n'.join(logs), """\
$1 = torch._ops.aten.diagonal($0)
$2 = torch._ops.aten.add($1, tensor([1., 1.]))
$3 = torch._ops.aten.diagonal_scatter($0, $2)
$4 = torch._ops.aten.mul($3, $3)""")

    def test_diagonal_mutated_input(self):
        def f(x):
            # simple test: there are pending updates afterwards, which the test syncs manually
            tmp = torch.ones(2)
            y = x.diagonal()
            y.add_(tmp)
            return x
        x = torch.ones(2, 2)
        self.assert_functionalization(f, x)

    def test_copy__functionalized(self):
        def f(x):
            # simple test: functionalizing copy_() should call its out-of-place variant
            tmp = torch.zeros(2, 2)
            # NOTE: LoggingTensor isn't a mode, which means that the diagonal call
            # will not be logged. This is fine for testing.
            tmp_slice = tmp.diagonal()
            tmp_slice.copy_(x)
            return x
        self.assert_functionalization(f, torch.ones(2))
        logs = self.get_logs(f, torch.ones(2))
        self.assertExpectedInline('\n'.join(logs), """$1 = torch._ops.aten.expand($0, [2])""")

    def test_split(self):
        def f(x):
            # test: view ops that return multiple tensors (split)
            tmp = torch.ones(2)
            y1, y2 = x.split(2)
            y3 = y2.diagonal()
            y3.add_(tmp)
            z = x * x
            return y3
        self.assert_functionalization(f, torch.ones(4, 2))
        logs = self.get_logs(f, torch.ones(4, 2))
        self.assertExpectedInline('\n'.join(logs), """\
$1, $2 = torch._ops.aten.split($0, 2)
$3 = torch._ops.aten.diagonal($2)
$4 = torch._ops.aten.add($3, tensor([1., 1.]))
$5, $6 = torch._ops.aten.split($0, 2)
$7 = torch._ops.aten.diagonal_scatter($6, $4)
$8 = torch._ops.aten.slice_scatter($0, $7, 0, 2, 4)
$9 = torch._ops.aten.mul($8, $8)""")

    def test_view_inplace(self):
        def f(x):
            # test: view + inplace op (transpose_)
            tmp = torch.ones(4)
            x.transpose_(1, 0)
            y = x[0]
            y.add_(tmp)
            return y
        self.assert_functionalization(f, torch.ones(4, 2))
        logs = self.get_logs(f, torch.ones(4, 2))
        self.assertExpectedInline('\n'.join(logs), """\
$1 = torch._ops.aten.transpose($0, 1, 0)
$2 = torch._ops.aten.select($1, 0, 0)
$3 = torch._ops.aten.add($2, tensor([1., 1., 1., 1.]))""")

    def test_everything(self):
        def f(x):
            # test: everything
            tmp = torch.ones(2, 2)
            y = x.view(8)
            z0 = y.reshape(2, 4)
            z1 = z0.transpose(1, 0)
            z1.unsqueeze_(0)
            z1.squeeze_()
            z2, z3 = z1.split(2)
            z2.add_(tmp)
            z4 = z0[0] + z2.reshape(4)
            return z2
        self.assert_functionalization(f, torch.ones(4, 2))
        logs = self.get_logs(f, torch.ones(4, 2))
        self.assertExpectedInline('\n'.join(logs), """\
$1 = torch._ops.aten.view($0, [8])
$2 = torch._ops.aten._reshape_alias($1, [2, 4], [4, 1])
$3 = torch._ops.aten.transpose($2, 1, 0)
$4 = torch._ops.aten.view($0, [8])
$5 = torch._ops.aten._reshape_alias($4, [2, 4], [4, 1])
$6 = torch._ops.aten.transpose($5, 1, 0)
$7 = torch._ops.aten.unsqueeze($6, 0)
$8 = torch._ops.aten.view($0, [8])
$9 = torch._ops.aten._reshape_alias($8, [2, 4], [4, 1])
$10 = torch._ops.aten.transpose($9, 1, 0)
$11 = torch._ops.aten.unsqueeze($10, 0)
$12 = torch._ops.aten.squeeze($11)
$13, $14 = torch._ops.aten.split($12, 2)
$15 = torch._ops.aten.add($13, tensor([[1., 1.],
        [1., 1.]]))
$16 = torch._ops.aten.select($2, 0, 0)
$17 = torch._ops.aten.clone($15, memory_format=0)
$18 = torch._ops.aten._unsafe_view($17, [4])
$19 = torch._ops.aten.view($0, [8])
$20 = torch._ops.aten._reshape_alias($19, [2, 4], [4, 1])
$21 = torch._ops.aten.transpose($20, 1, 0)
$22 = torch._ops.aten.unsqueeze($21, 0)
$23 = torch._ops.aten.squeeze($22)
$24 = torch._ops.aten.slice_scatter($23, $15, 0, 0, 2)
$25 = torch._ops.aten.unsqueeze($24, 0)
$26 = torch._ops.aten.squeeze($25, 0)
$27 = torch._ops.aten.transpose($26, 1, 0)
$28 = torch._ops.aten._reshape_alias($27, [8], [1])
$29 = torch._ops.aten.view($28, [4, 2])
$30 = torch._ops.aten.view($29, [8])
$31 = torch._ops.aten._reshape_alias($30, [2, 4], [4, 1])
$32 = torch._ops.aten.select($31, 0, 0)
$33 = torch._ops.aten.add($32, $18)""")

    def test_aliases_maintained_after_pass(self):
        def f(x):
            tmp = torch.ones(4, 2)
            y = x.view(4, 2)
            z = x.view(4, 2)
            y.add_(tmp)
            return y, z

        input_functional = torch._to_functional_tensor(torch.ones(4, 2))
        torch._enable_functionalization()
        try:
            y, z = f(input_functional)
            torch._sync(y)
            torch._sync(z)
        finally:
            torch._disable_functionalization()

        # y and z are aliases inside of the function, and that aliasing relationship should be maintained.
        _y = torch._from_functional_tensor(y)
        _z = torch._from_functional_tensor(z)
        self.assertTrue(are_aliased(_y, _z))

if __name__ == '__main__':
    run_tests()
